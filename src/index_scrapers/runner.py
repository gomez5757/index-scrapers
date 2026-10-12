"""Bounded HTTP discovery, persistent cross-portal identity and reviews."""
import csv
from copy import deepcopy
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import httpx
from selectolax.parser import HTMLParser

from .dedup import assign_property, perceptual_hash
from .extractors import DOMAINS, PortalBlocked, detail_price, parse, trusted_image_url, trusted_url

MAJOR = {"28079", "08019", "46250", "41091", "29067", "48020"}


def prioritize(targets):
    return sorted(targets, key=lambda target: (target["operation"] != "rent", 0 if target["municipality"] in MAJOR else 1 if target.get("priority") == "capital" else 2, target["municipality"], target["portal"]))


def image_hash(client, url):
    if not trusted_image_url(url):
        return None
    try:
        with client.stream("GET", url, follow_redirects=False) as response:
            response.raise_for_status()
            if not response.headers.get("content-type", "").startswith("image/"):
                return None
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 3_000_000:
                    return None
            return perceptual_hash(bytes(content))
    except (httpx.HTTPError, ValueError, OSError, ImportError):
        return None


def discover(state, client, targets, now, max_pages=3, hash_images=True, resume=False):
    report = {"results": 0, "changed": 0, "anomalies": []}
    for target in prioritize(targets):
        key = f"initial:{target['portal']}:{target['municipality']}:{target['operation']}"
        cursor = state.progress(key) if resume else None
        if cursor and cursor.get("done"):
            continue
        url = cursor.get("url") if cursor else target["url"]
        visited = set()
        for _ in range(max_pages):
            if url in visited:
                report["anomalies"].append({"portal": target["portal"], "municipality": target["municipality"], "reason": "pagination_cycle"})
                break
            visited.add(url)
            try:
                response = client.get(url, follow_redirects=True)
                if response.status_code != 200:
                    reason = f"http_{response.status_code}"
                    raise PortalBlocked(reason)
                if not trusted_url(target["portal"], str(response.url)):
                    raise PortalBlocked("unexpected_redirect")
                rows = parse(target["portal"], response.text, target)
                if not rows:
                    raise PortalBlocked("zero_results")
                report["results"] += len(rows)
                known_images = {item["id"]: item for item in state.catalog()}
                for candidate in rows:
                    if hash_images and candidate["property"]["cover_image"]:
                        known = known_images.get(candidate["id"])
                        if known and known.get("photo_hash") and known["property"].get("cover_image") == candidate["property"]["cover_image"]:
                            candidate["photo_hash"] = known["photo_hash"]
                        else:
                            candidate["photo_hash"] = image_hash(client, candidate["property"]["cover_image"])
                    report["changed"] += int(stage(state, candidate, now))
                missing = sum(not row["title"] or not row["images"] or row["property"]["area_m2"] is None or row["property"]["rooms"] is None for row in rows)
                if missing / len(rows) > .25:
                    report["anomalies"].append({"portal": target["portal"], "municipality": target["municipality"], "reason": "missing_fields", "count": missing})
                tree = HTMLParser(response.text)
                link = tree.css_first('a[rel="next"], link[rel="next"], a[aria-label="Siguiente"], a[title="Siguiente"]')
                next_page = trusted_url(target["portal"], link.attributes.get("href")) if link else None
                if resume:
                    state.progress(key, {"url": next_page or url, "done": next_page is None})
                if not next_page:
                    break
                url = next_page
            except (httpx.HTTPError, PortalBlocked) as exc:
                reason = str(exc) if isinstance(exc, PortalBlocked) else type(exc).__name__
                report["anomalies"].append({"portal": target["portal"], "municipality": target["municipality"], "reason": reason})
                break
    return report


def stage(state, candidate, now):
    candidate = deepcopy(candidate)
    catalog = state.catalog()
    existing = next((item for item in catalog if item["id"] == candidate["id"]), None)
    candidate["first_seen"] = existing["first_seen"] if existing else candidate.get("first_seen", now)
    candidate["last_seen"] = candidate["last_checked"] = now
    if existing and not candidate.get("photo_hash") and candidate["property"].get("cover_image") == existing["property"].get("cover_image"):
        candidate["photo_hash"] = existing.get("photo_hash")
    candidate["property_id"] = assign_property(candidate, catalog)
    return state.observe(candidate)


def review(state, client, operation, now, mode="prices", limit=100):
    report = {"checked": 0, "changed": 0, "anomalies": []}
    catalog = [row for row in state.catalog() if row["status"] == "active" and (operation is None or row["property"]["operation"] == operation)]
    catalog.sort(key=lambda row: (row["last_checked"], row["id"]))
    for known in catalog[:limit]:
        try:
            response = client.get(known["url"], follow_redirects=True)
            if trusted_url(known["portal"], str(response.url)) != trusted_url(known["portal"], known["url"]):
                raise PortalBlocked("unexpected_listing_redirect")
            report["checked"] += 1
            if response.status_code in {404, 410}:
                report["changed"] += int(state.check(known["id"], response.status_code, now))
                continue
            if response.status_code != 200:
                raise PortalBlocked(f"http_{response.status_code}")
            rows = parse(known["portal"], response.text, known["property"])
            state.check(known["id"], 200, now)
            if mode == "prices":
                found = next((item for item in rows if item["id"] == known["id"]), None)
                price = found["price"] if found else detail_price(known["portal"], response.text, known["id"])
                if price is None:
                    report["anomalies"].append({"portal": known["portal"], "reason": "detail_price_missing"})
                else:
                    updated = deepcopy(known)
                    updated["price"] = price
                    report["changed"] += int(stage(state, updated, now))
        except (httpx.HTTPError, PortalBlocked) as exc:
            state.check(known["id"], None, now)
            report["anomalies"].append({"portal": known["portal"], "reason": str(exc) if isinstance(exc, PortalBlocked) else type(exc).__name__})
    return report


def load_targets(path, municipalities):
    with Path(municipalities).open(encoding="utf-8", newline="") as source:
        codes = {(row["province"], row["municipality"]): row["name"] for row in csv.DictReader(source)}
    targets = json.loads(Path(path).read_text(encoding="utf-8"))
    for target in targets:
        if (target["province"], target["municipality"]) not in codes or target["municipality"][:2] != target["province"]:
            raise ValueError("target municipality absent from official INE dataset")
        if target["operation"] not in {"rent", "sale"} or target["portal"] not in DOMAINS or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", target["city_slug"]):
            raise ValueError("invalid target")
        if not trusted_url(target["portal"], target["url"]):
            raise ValueError("target URL must belong to its portal")
        target["name"] = codes[(target["province"], target["municipality"])]
    return prioritize(targets)
