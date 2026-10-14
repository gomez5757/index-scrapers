"""Bounded discovery of observed housing links; registry promotion is a separate step."""

import csv
import hashlib
import json
import os
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from html import escape
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
SUPPORTED_PORTALS = ("idealista", "fotocasa", "habitaclia")
# These five public directory URLs were observed on the primary sites, not expanded from a slug matrix.
DEFAULT_SEEDS = (
    "https://www.idealista.com/long-term-rentals-in-spain",
    "https://www.idealista.com/properties-for-sale-in-spain",
    "https://www.pisos.com/",
    "https://www.fotocasa.es/es/viviendas-de-alquiler-en-espana",
    "https://www.habitaclia.com/",
)
PROVINCES_SOURCE = "https://www.ine.es/daco/daco42/codmun/cod_provincia.htm"
CAPITALS_SOURCE = "https://www.boe.es/diario_boe/txt.php?id=BOE-A-2025-25362"
OPERATIONS = {"alquiler": "rent", "comprar": "sale"}
CHECKPOINT_VERSION = 2
EXECUTION_MODES = {"http", "fixture_replay"}


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
    if portal == "idealista":
        raw = urlsplit(href)
        # urljoin removes dot segments; reject them while the published href is intact.
        if (raw.scheme and raw.scheme != "https" or raw.netloc and raw.hostname not in HOSTS[portal]
                or raw.username is not None or raw.password is not None or raw.port is not None
                or raw.query or "\\" in href or re.search(r"[\x00-\x20]", href)
                or any(part in {".", ".."} for part in raw.path.split("/"))
                or re.search(r"%(?:2e|2f|5c|25)", raw.path, re.IGNORECASE)):
            raise ValueError("Unsafe Idealista directory URL")
    parsed = urlsplit(urljoin(source, href))
    if parsed.scheme != "https" or parsed.hostname not in HOSTS[portal] or parsed.username is not None or parsed.password is not None or parsed.port is not None or parsed.query or "\\" in href or re.search(r"[\x00-\x20]", href):
        raise ValueError("URL must use HTTPS and an exact allowlisted portal host without credentials, port, query or whitespace")
    return urlunsplit(("https", parsed.hostname, parsed.path or "/", "", ""))


def _portal(url):
    host = urlsplit(url).hostname
    return next((portal for portal, hosts in HOSTS.items() if host in hosts), None)


def _normalized_seed_urls(seeds):
    normalized = []
    for url in seeds:
        portal = _portal(url)
        if portal is None:
            raise ValueError("Seed outside portal allowlist")
        url = _safe_url(url, url, portal)
        if url not in normalized:
            normalized.append(url)
    return normalized


def _idealista_province_code(name, geography):
    # These historical province names were observed in Idealista's directory.
    aliases = {"guipuzcoa": "20", "vizcaya": "48"}
    province = aliases.get(name) or geography.province_code(name)
    if province is None or name in aliases:
        return province
    # A bilingual combined name can also be a municipality/province pair (Valencia-València).
    return province if any(_name_key(name) in _aliases(part) for part in geography.provinces[province].split("/")) else None


def _idealista_route(path, geography):
    if path in {"/long-term-rentals-in-spain", "/long-term-rentals-in-spain/"}:
        return {"scope": "country", "operation": "rent"}
    if path in {"/properties-for-sale-in-spain", "/properties-for-sale-in-spain/"}:
        return {"scope": "country", "operation": "sale"}
    match = re.fullmatch(r"/(alquiler|venta)-viviendas/(?:([a-z0-9]+(?:-[a-z0-9]+)*)(?:/([a-z0-9]+(?:-[a-z0-9]+)*))?/?)?", path)
    if not match:
        return None
    operation, location, area = match.groups()
    operation = "rent" if operation == "alquiler" else "sale"
    if location is None:
        return {"scope": "country", "operation": operation}
    if area is not None and area.startswith("con-"):
        return None
    province = _idealista_province_code(location.removesuffix("-provincia"), geography)
    if area is not None:
        # Only a province root can introduce an area; municipal filters are not directories.
        if province is None and not location.endswith("-provincia"):
            return None
        return {"scope": "area", "operation": operation, "province": province}
    if province is not None or location.endswith("-provincia"):
        return {"scope": "province", "operation": operation, "province": province}
    parts = location.split("-")
    municipal_routes = []
    for offset in range(1, len(parts)):
        province = _idealista_province_code("-".join(parts[offset:]), geography)
        if province is not None:
            municipal_routes.append(("-".join(parts[:offset]), province))
    return {"scope": "municipality", "operation": operation, "municipal_routes": municipal_routes}


def _route(portal, url, geography):
    path = urlsplit(url).path
    if portal == "idealista":
        return _idealista_route(path, geography)
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
            if "/viviendas/" in href or portal == "idealista" and ("-viviendas/" in href or "/long-term-rentals-in-spain" in href or "/properties-for-sale-in-spain" in href):
                excluded.append({"portal": portal, "url": href, "reason": "unsafe_url", **evidence})
            continue
        route = _route(portal, url, geography)
        if route is None:
            if "/viviendas/" in url or portal == "idealista" and ("-viviendas/" in url or "/long-term-rentals-in-spain" in url or "/properties-for-sale-in-spain" in url):
                excluded.append({"portal": portal, "url": url, "reason": "unsupported_or_filtered_route", **evidence})
            continue
        if ((route["scope"] == "province" or portal == "habitaclia") and route.get("province") is None
                or portal == "idealista" and route["scope"] != "country" and not (route.get("province") or route.get("municipal_routes"))):
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
        if portal == "idealista":
            candidates = {code for name, province in route["municipal_routes"]
                          for code in geography.names.get(_name_key(name), ()) if code[:2] == province}
        else:
            candidates = set(geography.names.get(_name_key(route["name"]), set()))
            if route.get("province"):
                candidates = {c for c in candidates if c[:2] == route["province"]}
        # A known conflicting anchor label is evidence against an otherwise matching slug.
        label_candidates = geography.names.get(_name_key(label), set())
        if label_candidates and (candidates or portal != "idealista") and not candidates.intersection(label_candidates):
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


def _checkpoint_check(condition, field):
    if not condition:
        raise ValueError("Invalid checkpoint " + field)


def _checkpoint_url(portal, value, field):
    _checkpoint_check(isinstance(portal, str) and portal in HOSTS and isinstance(value, str), field)
    try:
        canonical = _safe_url(value, value, portal)
    except ValueError as error:
        raise ValueError("Invalid checkpoint " + field) from error
    _checkpoint_check(value == canonical, field)


def _evidence_result(evidence, portal, pages, geography):
    fields = {"discovered_from", "source_sha256", "href", "label"}
    _checkpoint_check(isinstance(evidence, dict) and fields <= evidence.keys() <= fields | {"directory_group"}, "provenance fields")
    _checkpoint_check(all(isinstance(value, str) for value in evidence.values()), "provenance types")
    source = evidence["discovered_from"]
    _checkpoint_url(portal, source, "provenance source URL")
    page = pages.get((portal, source))
    _checkpoint_check(page is not None and page["outcome"] == "parsed" and page["sha256"] == evidence["source_sha256"], "provenance source/hash")
    anchor = '<a href="' + escape(evidence["href"], quote=True) + '">' + escape(evidence["label"]) + '</a>'
    if "directory_group" in evidence:
        anchor = '<div><h3>' + escape(evidence["directory_group"]) + '</h3><div data-testid="internal-links-content-child_areas">' + anchor + '</div></div>'
    # Re-run the existing route/name rules, not a second permissive matching implementation.
    result = parse_directory(portal, anchor, source, geography)
    for key, rows in result.items():
        for row in rows:
            if key == "excluded":
                row["source_sha256"] = evidence["source_sha256"]
            else:
                for provenance in row["provenance"]:
                    provenance["source_sha256"] = evidence["source_sha256"]
    return result


def _record_evidence(row, pages, geography, collection):
    _checkpoint_check(isinstance(row.get("provenance"), list) and bool(row["provenance"]), collection + " provenance")
    _checkpoint_check(isinstance(row.get("discovered_from"), list), collection + " discovered_from")
    results = [_evidence_result(evidence, row["portal"], pages, geography) for evidence in row["provenance"]]
    _checkpoint_check(row["provenance"] == _unique_rows(row["provenance"]), collection + " duplicate/unordered provenance")
    _checkpoint_check(row["discovered_from"] == sorted({e["discovered_from"] for e in row["provenance"]}), collection + " source references")
    return results


def _validate_frontier_entry(entry, pages, geography, seeds):
    _checkpoint_check(isinstance(entry, dict), "frontier record")
    _checkpoint_url(entry.get("portal"), entry.get("url"), "frontier URL")
    _checkpoint_check(type(entry.get("depth")) is int and entry["depth"] >= 0, "frontier depth")
    _checkpoint_check(isinstance(entry.get("scope"), str) and entry["scope"] in {"seed", "country", "province", "area", "municipality"}, "frontier scope")
    if entry["scope"] == "seed":
        _checkpoint_check(set(entry) == {"portal", "url", "scope", "depth", "seed_order", "discovered_from", "provenance"}, "seed fields")
        order = entry["seed_order"]
        _checkpoint_check(type(order) is int and 0 <= order < len(seeds) and seeds[order] == entry["url"] and entry["depth"] == 0, "seed identity")
        _checkpoint_check(isinstance(entry["provenance"], list) and isinstance(entry["discovered_from"], list), "seed provenance")
        if not entry["provenance"]:
            _checkpoint_check(entry["discovered_from"] == [], "seed source references")
            return
    results = _record_evidence(entry, pages, geography, "frontier")
    for result in results:
        expected = [row for row in result["frontier"] if row["url"] == entry["url"]]
        _checkpoint_check(len(expected) == 1, "frontier href/route")
        if entry["scope"] != "seed":
            omitted = {"depth", "discovered_from", "provenance"}
            _checkpoint_check({k: v for k, v in entry.items() if k not in omitted} == {k: v for k, v in expected[0].items() if k not in omitted}, "frontier operation/geography")
    if entry["scope"] != "seed":
        _checkpoint_check(entry["depth"] == 1 + min(pages[(entry["portal"], source)]["depth"] for source in entry["discovered_from"]), "frontier source depth")


def _validate_checkpoint(state, geography, execution_mode):
    _checkpoint_check(isinstance(state, dict), "root")
    if type(state.get("schema_version")) is not int or state["schema_version"] != CHECKPOINT_VERSION:
        raise ValueError("Unsupported checkpoint schema; version 1 has no execution mode/parser fingerprint. Preserve it and use a separate version 2 checkpoint")
    _checkpoint_check(state.get("execution_mode") == execution_mode, "execution mode; http and fixture_replay cannot share a checkpoint")
    _checkpoint_check(state.get("parser_sha256") == hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "parser fingerprint changed")
    _checkpoint_check(state.get("geography") == geography.identity(), "geography/input fingerprints changed")
    _checkpoint_check(set(state) == {"schema_version", "execution_mode", "parser_sha256", "geography", "seed_urls", "frontier", "targets", "pages", "excluded"}, "root fields")
    _checkpoint_check(isinstance(state["seed_urls"], list) and all(isinstance(url, str) for url in state["seed_urls"]), "seed URLs")
    _checkpoint_check(len(state["seed_urls"]) == len(set(state["seed_urls"])), "duplicate seed URLs")
    for url in state["seed_urls"]:
        _checkpoint_url(_portal(url), url, "seed URL")
    for collection in ("frontier", "targets", "pages", "excluded"):
        _checkpoint_check(isinstance(state[collection], list), collection + " collection")
    pages = {}
    for page in state["pages"]:
        required = {"portal", "url", "depth", "retrieved_at", "outcome", "request"}
        _checkpoint_check(isinstance(page, dict) and required <= page.keys() <= required | {"status", "sha256", "error_type"}, "pages fields")
        _checkpoint_url(page["portal"], page["url"], "pages URL")
        _validate_frontier_entry(page["request"], pages, geography, state["seed_urls"])
        _checkpoint_check(all(page[key] == page["request"][key] for key in ("portal", "url", "depth")), "pages request identity")
        try:
            _checkpoint_check(isinstance(page["retrieved_at"], str) and datetime.fromisoformat(page["retrieved_at"]).utcoffset() is not None, "pages timestamp")
        except (ValueError, TypeError) as error:
            raise ValueError("Invalid checkpoint pages timestamp") from error
        status, outcome = page.get("status"), page["outcome"]
        _checkpoint_check(isinstance(outcome, str), "pages outcome type")
        if outcome == "network_error":
            error_type = getattr(httpx, page.get("error_type"), None) if isinstance(page.get("error_type"), str) else None
            _checkpoint_check(isinstance(error_type, type) and issubclass(error_type, httpx.HTTPError) and "sha256" not in page, "pages network error")
            _checkpoint_check(status is None or type(status) is int and 100 <= status <= 599, "pages network status")
        else:
            _checkpoint_check(type(status) is int and 100 <= status <= 599 and "error_type" not in page, "pages HTTP status")
            if outcome in {"parsed", "unsupported_portal_directory"}:
                _checkpoint_check(status == 200 and isinstance(page.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", page["sha256"]) is not None, "pages parsed hash/status")
                _checkpoint_check((outcome == "parsed") == (page["portal"] in SUPPORTED_PORTALS), "pages parser outcome")
            else:
                valid = ((outcome == "blocked" and status in {401, 403, 429}) or (outcome == "redirect_requires_review" and 300 <= status < 400) or (outcome == "page_too_large" and status == 200) or (outcome == "http_error" and status != 200 and status not in {401, 403, 429} and not 300 <= status < 400))
                _checkpoint_check(valid and "sha256" not in page, "pages outcome/status")
        key = (page["portal"], page["url"])
        _checkpoint_check(key not in pages, "duplicate pages")
        pages[key] = page
    seen = set()
    for entry in state["frontier"]:
        _validate_frontier_entry(entry, pages, geography, state["seed_urls"])
        key = (entry["portal"], entry["url"])
        _checkpoint_check(key not in seen and key not in pages, "duplicate/already visited frontier")
        seen.add(key)
    seen = set()
    for target in state["targets"]:
        _checkpoint_check(isinstance(target, dict), "targets record")
        _checkpoint_url(target.get("portal"), target.get("url"), "targets URL")
        for result in _record_evidence(target, pages, geography, "targets"):
            expected = [row for row in result["targets"] if row["url"] == target["url"]]
            omitted = {"discovered_from", "provenance"}
            _checkpoint_check(len(expected) == 1 and {k: v for k, v in target.items() if k not in omitted} == {k: v for k, v in expected[0].items() if k not in omitted}, "targets URL/operation/INE/priority")
        key = (target["portal"], target["operation"], target["municipality"], target["url"])
        _checkpoint_check(key not in seen, "duplicate targets")
        seen.add(key)
    seen = set()
    for excluded in state["excluded"]:
        _checkpoint_check(isinstance(excluded, dict), "excluded record")
        portal, source = excluded.get("portal"), excluded.get("discovered_from")
        _checkpoint_url(portal, source, "excluded source")
        if excluded.get("reason") == "unsupported_portal_directory":
            page = pages.get((portal, source))
            _checkpoint_check(page is not None and page["outcome"] == "unsupported_portal_directory" and excluded == {"portal": portal, "url": source, "reason": "unsupported_portal_directory", "discovered_from": source}, "excluded unsupported parser")
        else:
            evidence = {key: excluded[key] for key in ("discovered_from", "source_sha256", "href", "label", "directory_group") if key in excluded}
            _checkpoint_check(excluded in _evidence_result(evidence, portal, pages, geography)["excluded"], "excluded reason/route/provenance")
        key = json.dumps(excluded, sort_keys=True, ensure_ascii=False)
        _checkpoint_check(key not in seen, "duplicate excluded records")
        seen.add(key)


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
    return {"schema_version": CHECKPOINT_VERSION, "execution_mode": state["execution_mode"], "parser_sha256": state["parser_sha256"],
            "validation": "observed_links_require_search_geography_validation_before_promotion",
            "geography": state["geography"], "targets": state["targets"], "excluded": state["excluded"],
            "pages": state["pages"], "coverage": coverage}


def run_discovery(client, geography, checkpoint_path, output_path, *, seeds=DEFAULT_SEEDS, max_pages=5, max_depth=2, max_page_bytes=4 * 1024 * 1024, execution_mode="http"):
    """Use one writer per checkpoint. Save each completed page before the next request.

    Failed/blocked URLs are recorded once and are not automatically retried. Deeper
    frontier entries remain durable for a later invocation with a larger max_depth.
    """
    if not isinstance(execution_mode, str) or execution_mode not in EXECUTION_MODES:
        raise ValueError("Execution mode must be http or fixture_replay")
    checkpoint_path, output_path = Path(checkpoint_path), Path(output_path)
    lock = checkpoint_path.with_name(checkpoint_path.name + ".lock")
    if output_path.resolve() == lock.resolve():
        raise ValueError("Output cannot replace the checkpoint lock")
    if max_pages < 1 or max_depth < 0 or max_page_bytes < 1 or checkpoint_path.resolve() == output_path.resolve():
        raise ValueError("Invalid work bounds or identical checkpoint/output paths")
    seed_urls = _normalized_seed_urls(seeds)
    original_bytes = None
    if checkpoint_path.exists():
        original_bytes = checkpoint_path.read_bytes()
        try:
            state = json.loads(original_bytes)
        except (ValueError, UnicodeError) as error:
            raise ValueError("Invalid checkpoint JSON; preserve it and use a separate checkpoint") from error
        _validate_checkpoint(state, geography, execution_mode)
        _checkpoint_check(state["seed_urls"] == seed_urls, "seed inputs changed; use a separate checkpoint")
    else:
        frontier = []
        for position, url in enumerate(seed_urls):
            portal = _portal(url)
            frontier.append({"portal": portal, "url": url, "scope": "seed", "depth": 0, "seed_order": position, "discovered_from": [], "provenance": []})
        state = {"schema_version": CHECKPOINT_VERSION, "execution_mode": execution_mode,
                 "parser_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "geography": geography.identity(), "seed_urls": seed_urls,
                 "frontier": _merge_frontier(frontier), "pages": [], "targets": [], "excluded": []}
        _validate_checkpoint(state, geography, execution_mode)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("x", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    try:
        if original_bytes is not None:
            if not checkpoint_path.exists() or checkpoint_path.read_bytes() != original_bytes:
                raise ValueError("Checkpoint changed before lock acquisition; retry the same checkpoint")
        elif checkpoint_path.exists():
            raise ValueError("Checkpoint appeared before lock acquisition; retry the same checkpoint")
        else:
            _atomic_json(checkpoint_path, state)
        pages_this_run = 0
        while pages_this_run < max_pages:
            position = next((i for i, e in enumerate(state["frontier"]) if e["depth"] <= max_depth), None)
            if position is None:
                break
            entry = state["frontier"][position]
            page = {"portal": entry["portal"], "url": entry["url"], "depth": entry["depth"], "request": entry,
                    "retrieved_at": datetime.now(timezone.utc).isoformat()}
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
