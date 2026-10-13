"""Bounded discovery of observed housing links; registry promotion is a separate step."""

import csv
import hashlib
import json
import os
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import httpx
from selectolax.parser import HTMLParser


HOSTS = {
    "idealista": {"idealista.com", "www.idealista.com"},
    "pisoscom": {"pisos.com", "www.pisos.com"},
    "fotocasa": {"fotocasa.es", "www.fotocasa.es"},
    "habitaclia": {"habitaclia.com", "www.habitaclia.com"},
}
SUPPORTED_PORTALS = ("fotocasa", "habitaclia")
# These four public directory URLs were observed on the primary sites, not expanded from a slug matrix.
DEFAULT_SEEDS = (
    "https://www.idealista.com/alquiler-viviendas/",
    "https://www.pisos.com/",
    "https://www.fotocasa.es/es/viviendas-de-alquiler-en-espana",
    "https://www.habitaclia.com/",
)
PROVINCES_SOURCE = "https://www.ine.es/daco/daco42/codmun/cod_provincia.htm"
CAPITALS_SOURCE = "https://www.boe.es/diario_boe/txt.php?id=BOE-A-2025-25362"
OPERATIONS = {"alquiler": "rent", "comprar": "sale"}


def _name_key(value):
    value = unicodedata.normalize("NFKD", unquote(value).casefold())
    return " ".join(re.sub(r"[^a-z0-9]+", " ", "".join(c for c in value if not unicodedata.combining(c))).split())


def _aliases(name):
    result = {_name_key(name)}
    parts = [p.strip() for p in name.split("/")]
    result.update(_name_key(p) for p in parts)
    if len(parts) == 2:
        result.add(_name_key(" ".join(reversed(parts))))
    # INE writes articles after a comma ("Coruña, A"). Do not reorder arbitrary comma-separated names.
    for part in parts:
        if "," in part:
            base, article = part.rsplit(",", 1)
            if _name_key(article) in {"a", "o", "as", "os", "el", "la", "los", "las", "l", "les", "els", "illes"}:
                result.add(_name_key(article + " " + base))
    return result - {""}


class MunicipalityIndex:
    def __init__(self, municipalities, provinces, capitals, fingerprints, major_codes=(), city_slugs=None):
        self.municipalities = municipalities
        self.provinces = provinces
        self.capitals = capitals
        self.fingerprints = fingerprints
        self.major_codes = set(major_codes)
        self.city_slugs = dict(city_slugs or {})
        if not self.major_codes <= municipalities.keys() or not self.city_slugs.keys() <= municipalities.keys():
            raise ValueError("Priority or city slug references an unknown INE municipality")
        self.names = {}
        self.province_names = {}
        for code, record in municipalities.items():
            for alias in _aliases(record["name"]):
                self.names.setdefault(alias, set()).add(code)
        for code, name in provinces.items():
            for alias in _aliases(name):
                self.province_names.setdefault(alias, set()).add(code)

    @classmethod
    def from_csv(cls, path, *, provinces_html, capitals_html=None, major_codes=(), city_slugs=None):
        path, provinces_html = Path(path), Path(provinces_html)
        fingerprints = {"municipalities": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "provinces": hashlib.sha256(provinces_html.read_bytes()).hexdigest()}
        municipalities = {}
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                code, province = row["municipality"], row["province"]
                if not re.fullmatch(r"\d{5}", code) or not re.fullmatch(r"\d{2}", province) or code[:2] != province or not row["name"] or code in municipalities:
                    raise ValueError("Invalid or duplicate INE municipality")
                municipalities[code] = {"municipality": code, "province": province, "name": row["name"]}
        provinces = {}
        for row in HTMLParser(provinces_html.read_bytes()).css("tr"):
            cells = row.css("td")
            if len(cells) == 2 and re.fullmatch(r"\d{2}", cells[0].text(strip=True)):
                provinces[cells[0].text(strip=True)] = cells[1].text(strip=True)
        if len(provinces) != 52 or not {r["province"] for r in municipalities.values()} <= provinces.keys():
            raise ValueError("Expected the official INE table of 52 provinces and autonomous cities")
        index = cls(municipalities, provinces, None, fingerprints, major_codes, city_slugs)
        if capitals_html is not None:
            data = Path(capitals_html).read_bytes()
            index.fingerprints["capitals"] = hashlib.sha256(data).hexdigest()
            names = []
            for table in HTMLParser(data).css("table"):
                headings = table.css("th")
                if headings and headings[0].text(strip=True) == "Capitales":
                    names = [r.css("td")[0].text(strip=True).rstrip(".") for r in table.css("tr") if len(r.css("td")) == 2]
            capitals = set()
            for name in names:
                codes = set().union(*(index.names.get(alias, set()) for alias in _aliases(name)))
                if len(codes) != 1:
                    raise ValueError("Official capital name has no unique current INE match: " + name)
                capitals.update(codes)
            if len(capitals) != 50:
                raise ValueError("Expected the official BOE table of 50 provincial capitals")
            index.capitals = capitals
        return index

    def province_code(self, name):
        candidates = self.province_names.get(_name_key(name), set())
        return next(iter(candidates)) if len(candidates) == 1 else None

    def identity(self):
        return {"sha256": self.fingerprints, "major_codes": sorted(self.major_codes), "city_slugs": self.city_slugs}


def _safe_url(href, source, portal):
    parsed = urlsplit(urljoin(source, href))
    if parsed.scheme != "https" or parsed.hostname not in HOSTS[portal] or parsed.username is not None or parsed.password is not None or parsed.port is not None or parsed.query or "\\" in href or re.search(r"[\x00-\x20]", href):
        raise ValueError("URL must use HTTPS and an exact allowlisted portal host without credentials, port, query or whitespace")
    return urlunsplit(("https", parsed.hostname, parsed.path or "/", "", ""))


def _portal(url):
    host = urlsplit(url).hostname
    return next((portal for portal, hosts in HOSTS.items() if host in hosts), None)


def _route(portal, url, geography):
    path = urlsplit(url).path
    if portal == "fotocasa":
        match = re.fullmatch(r"/es/(alquiler|comprar)/viviendas/([^/]+)/todas-las-zonas/l/?", path)
        if not match:
            return None
        operation, name = match.groups()
        if name == "espana":
            return {"scope": "country", "operation": OPERATIONS[operation]}
        if name.endswith("-provincia"):
            province = geography.province_code(name.removesuffix("-provincia"))
            return {"scope": "province", "operation": OPERATIONS[operation], "province": province}
        return {"scope": "municipality", "operation": OPERATIONS[operation], "name": name.removesuffix("-capital"), "province": None}
    if portal == "habitaclia":
        match = re.fullmatch(r"/(alquiler|comprar)/viviendas/([^/]+)-provincia/(?:([^/]+)/)?s/?", path)
        if not match:
            return None
        operation, province, name = match.groups()
        return {"scope": "municipality" if name else "province", "operation": OPERATIONS[operation],
                "name": name.removesuffix("-capital") if name else None, "province": geography.province_code(province)}
    return None


def _unique_rows(rows):
    return [json.loads(value) for value in sorted({json.dumps(row, sort_keys=True, ensure_ascii=False) for row in rows})]


def _target_sort(target):
    return (0 if target["operation"] == "rent" else 1,
            {"major": 0, "capital": 1, "rest": 2, "unknown": 3}[target["priority"]],
            target["province"], target["municipality"], target["portal"], target["url"])


def _area_directory_group(anchor):
    node = anchor.parent
    for _ in range(5):
        if node is None:
            break
        if node.attributes.get("data-testid") == "internal-links-content-child_areas":
            heading = node.parent.css_first("h3")
            label = heading.text(strip=True) if heading is not None else ""
            if {"comarcas", "islas"}.intersection(_name_key(label).split()):
                return label
        node = node.parent
    return None


def _merge_targets(targets):
    merged = {}
    for target in targets:
        key = (target["portal"], target["operation"], target["municipality"], target["url"])
        if key not in merged:
            merged[key] = {**target, "discovered_from": [], "provenance": []}
        merged[key]["discovered_from"] = sorted(set(merged[key]["discovered_from"] + target["discovered_from"]))
        merged[key]["provenance"] = _unique_rows(merged[key]["provenance"] + target["provenance"])
    return sorted(merged.values(), key=_target_sort)


def parse_directory(portal, html, source_url, geography):
    """Accept exact official-name matches, requiring explicit province to resolve ambiguity.

    Fotocasa municipal routes omit province: the source page's province is deliberately
    not inherited, because footer links can point to other provinces.
    """
    if portal not in HOSTS:
        raise ValueError("Unknown portal")
    source_url = _safe_url(source_url, source_url, portal)
    if portal not in SUPPORTED_PORTALS:
        return {"targets": [], "frontier": [], "excluded": [{"portal": portal, "url": source_url, "reason": "unsupported_portal_directory", "discovered_from": source_url}]}
    data = html.encode("utf-8") if isinstance(html, str) else html
    page_hash = hashlib.sha256(data).hexdigest()
    targets, frontier, excluded = [], [], []
    for anchor in HTMLParser(data).css("a[href]"):
        href = anchor.attributes["href"]
        label = anchor.text(strip=True)
        evidence = {"discovered_from": source_url, "source_sha256": page_hash, "href": href, "label": label}
        try:
            url = _safe_url(href, source_url, portal)
        except ValueError:
            if "/viviendas/" in href:
                excluded.append({"portal": portal, "url": href, "reason": "unsafe_url", **evidence})
            continue
        route = _route(portal, url, geography)
        if route is None:
            if "/viviendas/" in url:
                excluded.append({"portal": portal, "url": url, "reason": "unsupported_or_filtered_route", **evidence})
            continue
        if (route["scope"] == "province" or portal == "habitaclia") and route.get("province") is None:
            excluded.append({"portal": portal, "url": url, "reason": "unknown_province", **evidence})
            continue
        entry = {"portal": portal, "url": url, "scope": route["scope"], "operation": route["operation"], "discovered_from": [source_url], "provenance": [evidence]}
        if route["scope"] != "municipality":
            if route.get("province"):
                entry["province"] = route["province"]
            frontier.append(entry)
            continue
        group = _area_directory_group(anchor) if portal == "habitaclia" else None
        if group:
            # The public directory explicitly identifies these links as comarcas/islands.
            # Traverse them, preserving their label, without pretending they are municipalities.
            area_evidence = {**evidence, "directory_group": group}
            frontier.append({**entry, "scope": "area", "province": route["province"], "provenance": [area_evidence]})
            excluded.append({"portal": portal, "url": url, "reason": "non_municipal_area", **area_evidence})
            continue
        candidates = set(geography.names.get(_name_key(route["name"]), set()))
        if route.get("province"):
            candidates = {c for c in candidates if c[:2] == route["province"]}
        # A known conflicting anchor label is evidence against an otherwise matching slug.
        label_candidates = geography.names.get(_name_key(label), set())
        if label_candidates and not candidates.intersection(label_candidates):
            reason = "conflicting_label"
        elif len(candidates) != 1:
            reason = "ambiguous_municipality" if candidates else "unknown_municipality"
        else:
            reason = None
        if reason:
            excluded.append({"portal": portal, "url": url, "reason": reason, "candidates": sorted(candidates), **evidence})
            continue
        code = next(iter(candidates))
        municipality = geography.municipalities[code]
        priority = "major" if code in geography.major_codes else "unknown" if geography.capitals is None else "capital" if code in geography.capitals else "rest"
        target = {"portal": portal, "operation": route["operation"], "province": municipality["province"], "municipality": code,
                  "municipality_name": municipality["name"], "city_slug": geography.city_slugs.get(code, _name_key(municipality["name"]).replace(" ", "-")),
                  "priority": priority, "url": url, "validation": "observed_link", "discovered_from": [source_url], "provenance": [evidence]}
        if geography.capitals is not None:
            target["capital_provenance"] = {"source": CAPITALS_SOURCE, "sha256": geography.fingerprints["capitals"], "is_capital": code in geography.capitals}
        targets.append(target)
        frontier.append(entry)
    return {"targets": _merge_targets(targets), "frontier": _merge_frontier(frontier), "excluded": _unique_rows(excluded)}


def _merge_frontier(entries):
    merged = {}
    for entry in entries:
        key = (entry["portal"], entry["url"])
        if key not in merged:
            merged[key] = {**entry, "discovered_from": [], "provenance": []}
        merged[key]["discovered_from"] = sorted(set(merged[key]["discovered_from"] + entry["discovered_from"]))
        merged[key]["provenance"] = _unique_rows(merged[key]["provenance"] + entry["provenance"])
        if "depth" in entry:
            merged[key]["depth"] = min(entry["depth"], merged[key]["depth"])
    return sorted(merged.values(), key=lambda e: (e.get("depth", 0), {"seed": 0, "country": 1, "province": 2, "area": 3, "municipality": 4}[e["scope"]], e.get("seed_order", 0), 0 if e.get("operation") == "rent" else 1, e["portal"], e["url"]))


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _observed(state, geography):
    expected = len(geography.municipalities) * 2
    portals = {}
    for portal in HOSTS:
        pairs = {(t["operation"], t["municipality"]) for t in state["targets"] if t["portal"] == portal}
        portals[portal] = {"observed_pairs": len(pairs), "missing_pairs": expected - len(pairs),
                           "directory_parser_supported": portal in SUPPORTED_PORTALS}
    coverage = {"municipalities_expected": len(geography.municipalities), "operations": ["rent", "sale"],
                "expected_pairs_per_portal": expected, "portals": portals,
                "national_complete": all(p["missing_pairs"] == 0 for p in portals.values())}
    return {"schema_version": 1, "validation": "observed_links_require_search_geography_validation_before_promotion",
            "geography": state["geography"], "targets": state["targets"], "excluded": state["excluded"],
            "pages": state["pages"], "coverage": coverage}


def run_discovery(client, geography, checkpoint_path, output_path, *, seeds=DEFAULT_SEEDS, max_pages=5, max_depth=2, max_page_bytes=4 * 1024 * 1024):
    """Use one writer per checkpoint. Save each completed page before the next request.

    Failed/blocked URLs are recorded once and are not automatically retried. Deeper
    frontier entries remain durable for a later invocation with a larger max_depth.
    """
    checkpoint_path, output_path = Path(checkpoint_path), Path(output_path)
    lock = checkpoint_path.with_name(checkpoint_path.name + ".lock")
    if output_path.resolve() == lock.resolve():
        raise ValueError("Output cannot replace the checkpoint lock")
    if max_pages < 1 or max_depth < 0 or max_page_bytes < 1 or checkpoint_path.resolve() == output_path.resolve():
        raise ValueError("Invalid work bounds or identical checkpoint/output paths")
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("x", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    try:
        if checkpoint_path.exists():
            state = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if state.get("schema_version") != 1 or state.get("geography") != geography.identity():
                raise ValueError("Checkpoint schema or geography inputs changed; use a separate checkpoint")
        else:
            frontier = []
            for position, url in enumerate(seeds):
                portal = _portal(url)
                if portal is None:
                    raise ValueError("Seed outside portal allowlist")
                url = _safe_url(url, url, portal)
                frontier.append({"portal": portal, "url": url, "scope": "seed", "depth": 0, "seed_order": position, "discovered_from": [], "provenance": []})
            state = {"schema_version": 1, "geography": geography.identity(), "frontier": _merge_frontier(frontier), "pages": [], "targets": [], "excluded": []}
            _atomic_json(checkpoint_path, state)
        for entry in state["frontier"]:
            _safe_url(entry["url"], entry["url"], entry["portal"])
        pages_this_run = 0
        while pages_this_run < max_pages:
            position = next((i for i, e in enumerate(state["frontier"]) if e["depth"] <= max_depth), None)
            if position is None:
                break
            entry = state["frontier"][position]
            page = {"portal": entry["portal"], "url": entry["url"], "depth": entry["depth"], "retrieved_at": datetime.now(timezone.utc).isoformat()}
            parsed = None
            try:
                client.cookies.clear()
                with client.stream("GET", entry["url"], follow_redirects=False) as response:
                    page["status"] = response.status_code
                    if response.status_code in {401, 403, 429}:
                        page["outcome"] = "blocked"
                    elif 300 <= response.status_code < 400:
                        page["outcome"] = "redirect_requires_review"
                    elif response.status_code != 200:
                        page["outcome"] = "http_error"
                    else:
                        data = bytearray()
                        for chunk in response.iter_bytes(chunk_size=min(65536, max_page_bytes + 1)):
                            data.extend(chunk)
                            if len(data) > max_page_bytes:
                                break
                        if len(data) > max_page_bytes:
                            page["outcome"] = "page_too_large"
                        else:
                            page["sha256"] = hashlib.sha256(data).hexdigest()
                            parsed = parse_directory(entry["portal"], bytes(data), entry["url"], geography)
                            page["outcome"] = "parsed" if entry["portal"] in SUPPORTED_PORTALS else "unsupported_portal_directory"
                client.cookies.clear()
            except httpx.HTTPError as error:
                page["outcome"] = "network_error"
                page["error_type"] = type(error).__name__
            state["frontier"].pop(position)
            state["pages"].append(page)
            if parsed is not None:
                state["targets"] = _merge_targets(state["targets"] + parsed["targets"])
                state["excluded"] = _unique_rows(state["excluded"] + parsed["excluded"])
                visited = {(p["portal"], p["url"]) for p in state["pages"]}
                next_entries = [{**e, "depth": entry["depth"] + 1} for e in parsed["frontier"] if (e["portal"], e["url"]) not in visited]
                state["frontier"] = _merge_frontier(state["frontier"] + next_entries)
            _atomic_json(checkpoint_path, state)
            _atomic_json(output_path, _observed(state, geography))
            pages_this_run += 1
        observed = _observed(state, geography)
        _atomic_json(output_path, observed)
        return {"pages_this_run": pages_this_run, "pages_total": len(state["pages"]), "frontier_pending": len(state["frontier"]),
                "targets_observed": len(state["targets"]), "excluded": len(state["excluded"]),
                "gaps": [p for p in state["pages"] if p["outcome"] != "parsed"],
                "national_complete": observed["coverage"]["national_complete"]}
    finally:
        lock.unlink(missing_ok=True)
