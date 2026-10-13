"""Bounded HTTP discovery, persistent cross-portal identity and reviews."""
import csv
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
from time import monotonic
from urllib.parse import urlsplit

import httpx
from selectolax.parser import HTMLParser

from .dedup import assign_property, perceptual_hash
from .extractors import DOMAINS, PortalBlocked, detail_price, initial_data, parse, trusted_image_url, trusted_url

MAJOR = {"28079", "08019", "46250", "41091", "29067", "48020"}


class InitialTimeLimit(Exception):
    pass


def prioritize(targets):
    return sorted(targets, key=lambda target: (target["operation"] != "rent", 0 if target["municipality"] in MAJOR else 1 if target.get("priority") == "capital" else 2, target["municipality"], target["portal"]))


def image_hash(client, url, deadline=None):
    if not trusted_image_url(url):
        return None
    try:
        options = {"timeout": min(25, max(.001, deadline - monotonic()))} if deadline is not None else {}
        with client.stream("GET", url, follow_redirects=False, **options) as response:
            response.raise_for_status()
            if not response.headers.get("content-type", "").startswith("image/"):
                return None
            content = bytearray()
            for chunk in response.iter_bytes():
                if deadline is not None and monotonic() >= deadline:
                    raise InitialTimeLimit
                content.extend(chunk)
                if len(content) > 3_000_000:
                    return None
            return perceptual_hash(bytes(content))
    except (httpx.HTTPError, ValueError, OSError, ImportError):
        return None


def _collect_page(state, client, target, url, now, report, hash_images, read_only, deadline=None):
    if deadline is None:
        request = nullcontext(client.get(url, follow_redirects=True))
    else:
        request = client.stream("GET", url, follow_redirects=True, timeout=min(25, max(.001, deadline - monotonic())))
    with request as response:
        if response.status_code != 200:
            raise PortalBlocked(f"http_{response.status_code}")
        if not trusted_url(target["portal"], str(response.url)):
            raise PortalBlocked("unexpected_redirect")
        if deadline is None:
            html = response.text
        else:
            content = bytearray()
            for chunk in response.iter_bytes():
                if monotonic() >= deadline:
                    raise InitialTimeLimit
                content.extend(chunk)
            html = content.decode(response.encoding or "utf-8", errors="replace")
    rows = parse(target["portal"], html, target)
    if not rows:
        raise PortalBlocked("zero_results")
    report["results"] += len(rows)
    if not read_only:
        known_images = {item["id"]: item for item in state.catalog()}
        for candidate in rows:
            if deadline is not None and monotonic() >= deadline:
                raise InitialTimeLimit
            if hash_images and candidate["property"]["cover_image"]:
                known = known_images.get(candidate["id"])
                if known and known.get("photo_hash") and known["property"].get("cover_image") == candidate["property"]["cover_image"]:
                    candidate["photo_hash"] = known["photo_hash"]
                else:
                    candidate["photo_hash"] = image_hash(client, candidate["property"]["cover_image"], deadline)
            if deadline is not None and monotonic() >= deadline:
                raise InitialTimeLimit
            report["changed"] += int(stage(state, candidate, now))
    missing = sum(not row["title"] or not row["images"] or row["property"]["area_m2"] is None or row["property"]["rooms"] is None for row in rows)
    if missing / len(rows) > .25:
        report["anomalies"].append({"portal": target["portal"], "municipality": target["municipality"], "reason": "missing_fields", "count": missing})
    return HTMLParser(html)


def _next_link(tree):
    return tree.css_first('a[rel="next"], link[rel="next"], a[aria-label="Siguiente"], a[title="Siguiente"]')


def _pagination(tree, portal):
    metadata = {}
    try:
        if portal == "fotocasa":
            script = tree.css_first("#__initial_props__")
            if not script:
                return metadata
            search = json.loads(script.text())["initialSearch"]
            result = search["result"]
            paged = result.get("resultsV2") or {}
            page = paged.get("page") or {}
            number = page.get("number", (search.get("params") or {}).get("pageNumber"))
            size = page.get("size")
            total = paged.get("totalItems", result.get("count"))
            pages = None
        elif portal == "habitaclia":
            data = initial_data(tree)
            if not data:
                return metadata
            page = data["initialSearchResultsPage"]["initialSearchContext"]["results"]["pagination"]
            number, size, total, pages = (page.get(key) for key in ("page", "pageSize", "totalCount", "totalPages"))
        else:
            return metadata
    except (ValueError, KeyError, TypeError, AttributeError):
        return metadata
    values = {"page": number, "page_size": size, "total_results": total, "total_pages": pages}
    metadata = {key: value for key, value in values.items() if isinstance(value, int) and not isinstance(value, bool) and value >= (0 if key == "total_results" else 1)}
    if "total_pages" not in metadata and "total_results" in metadata and "page_size" in metadata:
        metadata["total_pages"] = (metadata["total_results"] + metadata["page_size"] - 1) // metadata["page_size"]
    if "page" in metadata and "total_pages" in metadata:
        metadata["has_more"] = metadata["page"] < metadata["total_pages"]
    elif metadata.get("total_results", 0) > 0 or metadata.get("total_pages", 0) > 1:
        # An advertised total without a usable page boundary cannot prove exhaustion.
        metadata["has_more"] = True
    return metadata


def _phase(target):
    priority = "major" if target["municipality"] in MAJOR else "capital" if target.get("priority") == "capital" else "rest"
    return f"{target['operation']}:{priority}"


def _completion_status(cursors):
    statuses = {cursor["status"] for cursor in cursors}
    return "pending" if "pending" in statuses else "blocked" if "blocked" in statuses else "completed"


def _initial_cursors(state, targets):
    cursors, groups = {}, {}
    for target in prioritize(targets):
        key = f"initial:{target['portal']}:{target['municipality']}:{target['operation']}"
        cursor = state.progress(key) or {"url": target["url"], "done": False}
        cursor["status"] = cursor.get("status", "completed" if cursor.get("done") else "pending")
        cursors[key] = (target, cursor)
        groups.setdefault(_phase(target), []).append(key)
    return cursors, groups


def _eligible(cursor, current_time):
    if cursor["status"] == "pending":
        return True
    if cursor["status"] != "blocked":
        return False
    retry_after = cursor.get("retry_after")
    return not retry_after or datetime.fromisoformat(retry_after.replace("Z", "+00:00")) <= current_time


def _summarize_initial(cursors, groups, current_time):
    targets = []
    for key, (target, cursor) in cursors.items():
        targets.append({"key": key, "portal": target["portal"], "municipality": target["municipality"], "operation": target["operation"], "phase": _phase(target), **{name: cursor.get(name) for name in ("status", "url", "reason", "retry_after")}, "pages": cursor.get("pages", 0), "attempts": cursor.get("attempts", 0), "pagination": cursor.get("pagination", {})})
    phases = [{"phase": phase, "status": _completion_status([cursors[key][1] for key in keys]), **{name: sum(cursors[key][1]["status"] == name for key in keys) for name in ("pending", "completed", "blocked")}} for phase, keys in groups.items()]
    return {"status": _completion_status([cursor for _, cursor in cursors.values()]), **{name: sum(cursor["status"] == name for _, cursor in cursors.values()) for name in ("pending", "completed", "blocked")}, "eligible": sum(_eligible(cursor, current_time) for _, cursor in cursors.values()), "phase": next((phase for phase, keys in groups.items() if any(_eligible(cursors[key][1], current_time) for key in keys)), None), "targets": targets, "phases": phases}


def initial_status(state, targets, now):
    """Read configured-target completion and retry eligibility without changing state."""
    cursors, groups = _initial_cursors(state, targets)
    return _summarize_initial(cursors, groups, datetime.fromisoformat(now.replace("Z", "+00:00")))


def _discover_initial(state, client, targets, now, max_pages, hash_images, max_seconds):
    report = {"results": 0, "changed": 0, "anomalies": []}
    deadline = monotonic() + max_seconds
    current_time = datetime.fromisoformat(now.replace("Z", "+00:00"))
    cursors, groups = _initial_cursors(state, targets)
    pages, first_phase, stop_reason = 0, None, None
    while pages < max_pages:
        if monotonic() >= deadline:
            stop_reason = "time_limit"
            break
        selected = None
        for phase, keys in groups.items():
            eligible = [key for key in keys if _eligible(cursors[key][1], current_time)]
            if eligible:
                selected = min(eligible, key=lambda key: cursors[key][1].get("attempts", 0))
                first_phase = first_phase or phase
                break
        if selected is None:
            break
        target, cursor = cursors[selected]
        url = cursor.get("url") or target["url"]
        cursor.update(status="pending", done=False, reason=None, retry_after=None, last_attempt=now)
        cursor["attempts"] = cursor.get("attempts", 0) + 1
        try:
            if not trusted_url(target["portal"], url):
                raise PortalBlocked("invalid_cursor_url")
            pages += 1
            tree = _collect_page(state, client, target, url, now, report, hash_images, False, deadline)
            link = _next_link(tree)
            next_page = trusted_url(target["portal"], link.attributes.get("href")) if link else None
            cursor["pagination"] = _pagination(tree, target["portal"])
            visited = cursor.get("visited", [])
            if url not in visited:
                visited.append(url)
                cursor["pages"] = cursor.get("pages", 0) + 1
            cursor["visited"] = visited
            if link and not next_page:
                raise PortalBlocked("pagination_untrusted")
            if next_page in visited:
                raise PortalBlocked("pagination_cycle")
            if not next_page and cursor["pagination"].get("has_more"):
                raise PortalBlocked("pagination_missing")
            cursor.update(url=next_page or url, done=next_page is None, status="pending" if next_page else "completed")
        except InitialTimeLimit:
            cursor.update(url=url, reason="time_limit")
            stop_reason = "time_limit"
        except (httpx.HTTPError, PortalBlocked) as exc:
            reason = str(exc) if isinstance(exc, PortalBlocked) else type(exc).__name__
            retry_after = current_time + timedelta(hours=1)
            cursor.update(url=url, status="blocked", reason=reason, retry_after=retry_after.isoformat().replace("+00:00", "Z"))
        state.progress(selected, cursor)
        if stop_reason:
            break
    summary = _summarize_initial(cursors, groups, current_time)
    for target in summary["targets"]:
        if target["status"] == "blocked":
            report["anomalies"].append({name: target[name] for name in ("portal", "municipality", "operation", "reason")})
    report["initial"] = {**summary, "pages": pages, "phase": first_phase, "stop_reason": stop_reason or ("page_limit" if pages >= max_pages and summary["status"] == "pending" else summary["status"])}
    return report


def discover(state, client, targets, now, max_pages=3, hash_images=True, resume=False, read_only=False, max_seconds=240):
    if max_pages < 1 or max_seconds <= 0:
        raise ValueError("discovery limits must be positive")
    if resume and not read_only:
        return _discover_initial(state, client, targets, now, max_pages, hash_images, max_seconds)
    report = {"results": 0, "changed": 0, "anomalies": []}
    for target in prioritize(targets):
        url = target["url"]
        visited = set()
        for _ in range(max_pages):
            if url in visited:
                report["anomalies"].append({"portal": target["portal"], "municipality": target["municipality"], "reason": "pagination_cycle"})
                break
            visited.add(url)
            try:
                tree = _collect_page(state, client, target, url, now, report, hash_images, read_only)
                link = _next_link(tree)
                next_page = trusted_url(target["portal"], link.attributes.get("href")) if link else None
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


def _sweep_candidates(catalog, missing, sweep):
    if sweep["complete"]:
        return []
    skipped = {item["listing"]["id"] for item in missing} | set(sweep["checked"])
    return sorted((row for row in catalog if (sweep["cursor"] is None or row["id"] > sweep["cursor"]) and row["id"] <= sweep["upper_bound"] and row["id"] not in skipped), key=lambda row: row["id"])


def _due_rechecks(missing, now):
    current_time = datetime.fromisoformat(now.replace("Z", "+00:00"))
    due = [item for item in missing if current_time - datetime.fromisoformat(item["first_missing_at"].replace("Z", "+00:00")) >= timedelta(hours=1)]
    return sorted(due, key=lambda item: (datetime.fromisoformat(item["first_missing_at"].replace("Z", "+00:00")), item["listing"]["id"]))


def review(state, client, operation, now, mode="prices", limit=100):
    if limit < 1:
        raise ValueError("review limit must be positive")
    report = {"checked": 0, "changed": 0, "anomalies": []}
    catalog = [row for row in state.catalog() if row["status"] == "active" and (operation is None or row["property"]["operation"] == operation)]
    if mode == "withdrawals":
        key = f"withdrawals:{operation or 'all'}"
        sweep = state.progress(key)
        elapsed = datetime.fromisoformat(now.replace("Z", "+00:00")) - datetime.fromisoformat(sweep["started_at"].replace("Z", "+00:00")) if sweep else None
        if sweep is None or (sweep["complete"] and elapsed >= timedelta(hours=12)):
            sweep = {"started_at": now, "cursor": None, "upper_bound": max((row["id"] for row in catalog), default=""), "checked": [], "complete": not catalog}
            state.progress(key, sweep)
        missing = state.missing_checks(operation)
        due = _due_rechecks(missing, now)
        selected = [(item["listing"], False) for item in due[:limit]]
        selected += [(row, True) for row in _sweep_candidates(catalog, missing, sweep)[:limit - len(selected)]]
    else:
        catalog.sort(key=lambda row: (row["last_checked"], row["id"]))
        selected = [(row, False) for row in catalog[:limit]]
    for known, in_sweep in selected:
        handled = False
        try:
            response = client.get(known["url"], follow_redirects=True)
            if trusted_url(known["portal"], str(response.url)) != trusted_url(known["portal"], known["url"]):
                raise PortalBlocked("unexpected_listing_redirect")
            report["checked"] += 1
            if response.status_code in {404, 410}:
                report["changed"] += int(state.check(known["id"], response.status_code, now))
                handled = True
                continue
            if response.status_code != 200:
                raise PortalBlocked(f"http_{response.status_code}")
            rows = parse(known["portal"], response.text, known["property"])
            state.check(known["id"], 200, now)
            handled = True
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
            handled = True
            report["anomalies"].append({"portal": known["portal"], "reason": str(exc) if isinstance(exc, PortalBlocked) else type(exc).__name__})
        finally:
            if mode == "withdrawals" and handled:
                if in_sweep:
                    sweep["cursor"] = known["id"]
                    sweep["checked"] = [identifier for identifier in sweep["checked"] if identifier > sweep["cursor"]]
                elif not sweep["complete"] and (sweep["cursor"] is None or known["id"] > sweep["cursor"]) and known["id"] <= sweep["upper_bound"]:
                    # A due recheck can run ahead of the cursor; do not check it twice.
                    sweep["checked"].append(known["id"])
                state.progress(key, sweep)
    if mode == "withdrawals":
        catalog = [row for row in state.catalog() if row["status"] == "active" and (operation is None or row["property"]["operation"] == operation)]
        missing = state.missing_checks(operation)
        remaining = len(_sweep_candidates(catalog, missing, sweep))
        sweep["complete"] = remaining == 0
        state.progress(key, sweep)
        report["withdrawals"] = {"sweep_started_at": sweep["started_at"], "sweep_remaining": remaining, "due_rechecks": len(_due_rechecks(missing, now)), "awaiting_rechecks": len(missing), "limit": limit}
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
