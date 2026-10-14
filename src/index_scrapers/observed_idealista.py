"""Pure conversion of saved DOM observations; incomplete input remains reviewable.

``ready`` certifies a candidate's structure and observed evidence. Before calling
``runner.stage``, an importer must check the shared catalogue for chronology,
immutable identity and merging known fields. Direct replay can regress dates and
erase optional values from existing records. This module implements no importer
or catalogue gate. Identity belongs to deduplication after that gate, so no
property_id is produced here.
The envelope, including the untouched source and uncertainty, must be retained
separately: the delivery schema has no provenance or publication-date fields.
"""

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from urllib.parse import urlsplit

from .extractors import kind, row, trusted_image_url
from .target_discovery import MunicipalityIndex, _name_key


_DATE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z")
_TYPE = re.compile(
    r"^(piso|apartamento|estudio|ático|atico|dúplex|duplex|habitación|habitacion|"
    r"casa(?: o chalet)?(?: independiente)?|chalet(?: adosado| pareado| independiente)?|adosado)\b",
    re.IGNORECASE,
)
_AMOUNT = r"\d+(?:\.\d{3})*(?:,\d{1,2})?"
_NULL_PROPERTY = {
    "operation": None, "kind": None, "province": None, "municipality": None,
    "city_slug": None, "district": None, "lat": None, "lng": None,
    "area_m2": None, "rooms": None, "baths": None, "floor": None,
    "has_lift": None, "has_parking": None, "has_terrace": None,
    "furnished": None, "cover_image": None,
}


def _timestamp(value):
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        return None
    offset = re.search(r"[+-](\d{2}):(\d{2})$", value)
    if offset and (int(offset.group(1)) > 23 or int(offset.group(2)) > 59):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        return result if result.timestamp() >= 0 else None
    except (ValueError, OverflowError):
        return None


def _public_url(value):
    """Validate the original absolute URL without repairing or canonicalising it."""
    if not isinstance(value, str) or len(value) > 2048 or re.search(r"[\x00-\x20\\]", value):
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and parsed.hostname in {"www.idealista.com", "idealista.com"}
                and parsed.username is None and parsed.password is None and parsed.port is None
                and not parsed.query and not parsed.fragment
                and not any(part in {".", ".."} for part in parsed.path.split("/"))
                and not re.search(r"%(?:2e|2f|5c|25)", parsed.path, re.IGNORECASE))
    except ValueError:
        return False


def _integer(text, maximum):
    try:
        value = Decimal(text.replace(".", "").replace(",", "."))
        return int(value) if value == value.to_integral_value() and 0 <= value <= maximum else None
    except (InvalidOperation, ValueError):
        return None


def _measure(details, pattern, maximum, name, reasons):
    matches = [match.group(1) for detail in details for match in re.finditer(pattern, detail, re.IGNORECASE)]
    values = {_integer(match, maximum) for match in matches}
    if None in values or len(values) > 1:
        reasons.append("invalid_or_conflicting_" + name)
        return None
    return next(iter(values)) if values else None


def _geography(title, geography, reasons):
    locality = None
    if title:
        if "," in title:
            locality = title.rsplit(",", 1)[1].strip()
        else:
            match = _TYPE.match(title)
            if match and title[match.end():].lower().startswith(" en "):
                locality = title[match.end() + 4:].strip()
    candidates = sorted(geography.names.get(_name_key(locality), set())) if locality else []
    if len(candidates) != 1:
        reasons.append("ambiguous_municipality" if candidates else "unknown_municipality" if locality else "missing_municipality")
        return {}, {"observed_name": locality, "candidates": candidates}
    code = candidates[0]
    municipal = geography.municipalities[code]
    slug = geography.city_slugs.get(code, _name_key(municipal["name"]).replace(" ", "-"))
    if (not re.fullmatch(r"\d{5}", code) or municipal.get("province") != code[:2]
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", slug)):
        reasons.append("invalid_ine_identity")
        return {}, {"observed_name": locality, "candidates": candidates}
    return {"province": municipal["province"], "municipality": code, "city_slug": slug}, {
        "observed_name": locality, "candidates": candidates, "ine_name": municipal["name"],
        "ine_sources": deepcopy(geography.fingerprints),
    }


def _dates(record, origins, reasons, as_of):
    observed = record.get("observed_at")
    latest = _timestamp(observed)
    if latest is None:
        reasons.append("missing_observed_at" if observed is None else "invalid_observed_at")
    known = []
    unknown = False
    for origin in origins:
        value = origin.get("observed_at")
        parsed = _timestamp(value)
        if parsed is None:
            unknown = True
            if value is not None:
                reasons.append("invalid_origin_observed_at")
        else:
            known.append((parsed, value))
    if latest is not None:
        known.append((latest, observed))
    if unknown:
        reasons.append("first_seen_unknown")
    if latest is not None and known and latest < max(item[0] for item in known):
        reasons.append("observed_at_precedes_origin")
    if as_of is not None and any(item[0] > as_of for item in known):
        reasons.append("future_observed_at")
    earliest = min(known, key=lambda item: item[0])[1] if known else None
    return {
        "first_seen": earliest if not unknown else None,
        "last_seen": observed if latest is not None else None,
        "last_checked": observed if latest is not None else None,
    }, {"earliest_known_at": earliest, "has_undated_observations": unknown, "published_at": None}


def adapt_observed_card(record, geography: MunicipalityIndex, *, as_of=None):
    """Return one ready/pending envelope, never drop a record or perform I/O.

    ``as_of`` is an optional explicit capture audit cutoff, never the import time.
    Missing card text cannot be replaced by aggregate numeric fields. Unknown
    dates in the source history prevent claiming a known first_seen timestamp.
    A ready result still requires an importer's catalogue, chronology and merge
    gate before staging; this pure function cannot establish those conditions.
    """
    cutoff = _timestamp(as_of) if as_of is not None else None
    if as_of is not None and cutoff is None:
        raise ValueError("as_of must be an explicit timezone-aware ISO timestamp")
    reasons = []
    source = deepcopy(record)
    if not isinstance(record, Mapping):
        return {"status": "pending", "candidate": None, "reasons": ["invalid_record"], "evidence": {}, "source": source}
    card = record.get("source_card")
    if not isinstance(card, Mapping):
        card = {}
        reasons.append("missing_source_card")
    title = card.get("title") if isinstance(card.get("title"), str) and card["title"].strip() else None
    if title is None:
        reasons.append("missing_title")
    elif len(title) > 300:
        reasons.append("title_too_long")
    type_match = _TYPE.match(title or "")
    property_kind = kind(type_match.group()) if type_match else None
    if type_match and _name_key(type_match.group()) == "habitacion":
        property_kind = "room"
    if property_kind is None:
        reasons.append("unknown_kind")
    geo, geo_evidence = _geography(title, geography, reasons)

    url = card.get("url")
    valid_url = _public_url(url)
    listing_match = re.fullmatch(r"/inmueble/(\d+)/?", urlsplit(url).path) if valid_url else None
    if not listing_match:
        reasons.append("missing_listing_url" if url is None else "invalid_listing_url")
    identifiers = [value for value in (record.get("id"), card.get("id")) if value is not None]
    if listing_match:
        identifiers.append(listing_match.group(1))
    identifier = str(identifiers[0]) if identifiers else None
    if identifier is None:
        reasons.append("missing_listing_id")
    elif not re.fullmatch(r"\d+", identifier) or any(str(value) != identifier for value in identifiers):
        reasons.append("conflicting_listing_id")
    if identifier is not None and len("idealista:" + identifier) > 200:
        reasons.append("listing_id_too_long")

    raw_origins = record.get("origins", [])
    origins = [origin for origin in raw_origins if isinstance(origin, Mapping)] if isinstance(raw_origins, list) else []
    if not isinstance(raw_origins, list) or len(origins) != len(raw_origins):
        reasons.append("invalid_origins")
    field_origins = record.get("field_origins")
    relevant_origins = []
    if isinstance(field_origins, Mapping):
        relevant_origins = [origin for key in ("title", "url", "price_text", "details", "images")
                            if isinstance((origin := field_origins.get(key)), Mapping)]
    latest_observation = record.get("latest_observation")
    latest_origin = latest_observation.get("origin") if isinstance(latest_observation, Mapping) else None
    if isinstance(latest_origin, Mapping):
        relevant_origins.append(latest_origin)
    operation_origins = relevant_origins or origins
    pages = list(dict.fromkeys(origin.get("source_page") for origin in operation_origins if isinstance(origin.get("source_page"), str)))
    if not pages:
        reasons.append("missing_source_page")
    operations = set()
    for page in pages:
        if not _public_url(page):
            reasons.append("invalid_source_page")
            continue
        path = urlsplit(page).path
        operation = "rent" if path.startswith("/alquiler-viviendas/") else "sale" if path.startswith("/venta-viviendas/") else None
        if operation is None:
            reasons.append("unknown_operation")
        else:
            operations.add(operation)
    operation = next(iter(operations)) if len(operations) == 1 else None
    if len(operations) > 1:
        reasons.append("conflicting_operation")

    price_text = card.get("price_text")
    price_match = re.fullmatch(r"\s*(" + _AMOUNT + r")\s*€\s*(/\s*mes)?\s*", price_text, re.IGNORECASE) if isinstance(price_text, str) else None
    price = _integer(price_match.group(1), 1_000_000_000) if price_match else None
    if price is None or price <= 0:
        price = None
        reasons.append("missing_price_text" if price_text is None else "invalid_price_text")
    if price_match and operation is not None and bool(price_match.group(2)) != (operation == "rent"):
        reasons.append("price_operation_conflict")
    if record.get("price_eur") is not None and price is not None and record["price_eur"] != price:
        reasons.append("conflicting_price")

    raw_details = card.get("details")
    details = raw_details if isinstance(raw_details, list) and all(isinstance(item, str) for item in raw_details) else []
    if raw_details is not None and not details and raw_details != []:
        reasons.append("invalid_details")
    area = _measure(details, r"(?<![\d.,-])(" + _AMOUNT + r")\s*m[²2]\b", 100_000, "area_m2", reasons)
    rooms = _measure(details, r"(?<![\d.,-])(\d+)\s*hab(?:\.|itaciones?\b|\b)", 100, "rooms", reasons)
    baths = _measure(details, r"(?<![\d.,-])(\d+)\s*ba[ñn]os?\b", 100, "baths", reasons)
    lift_values = {match.group(1).lower() == "con" for detail in details for match in re.finditer(r"\b(con|sin) ascensor\b", detail, re.IGNORECASE)}
    if len(lift_values) > 1:
        reasons.append("conflicting_lift")
    for name, value in (("area_m2", area), ("rooms", rooms), ("bathrooms", baths)):
        if record.get(name) is not None and value is not None and record[name] != value:
            reasons.append("conflicting_" + name)

    raw_images = card.get("images")
    images = []
    if raw_images is not None:
        if not isinstance(raw_images, list):
            reasons.append("invalid_images")
        else:
            for image in raw_images:
                if not trusted_image_url(image) or len(image) > 2048 or re.search(r"[\x00-\x20\\]", image):
                    reasons.append("invalid_image_url")
                elif image not in images:
                    images.append(image)
            if len(images) > 30:
                reasons.append("too_many_observed_images")
                images = images[:30]
    dates, date_evidence = _dates(record, origins, reasons, cutoff)
    prop = dict(_NULL_PROPERTY, **geo, operation=operation, kind=property_kind,
                area_m2=area, rooms=rooms, baths=baths,
                has_lift=next(iter(lift_values)) if len(lift_values) == 1 else None,
                cover_image=images[0] if images else None)
    candidate = {
        "id": "idealista:" + identifier if identifier is not None else None,
        "portal": "idealista", "url": url if listing_match else None,
        "title": title, "description": None, "lang": None, "price": price,
        "images": images, "advertiser": None, "status": "active", "property": prop, **dates,
    }
    # Exercise the collector's normalizer only when its required core is known.
    # Its text defaults and lexical kind detection cannot establish missing facts.
    if listing_match and identifier and price is not None and operation and geo:
        normalized = row("idealista", identifier, url, title, price, dict(geo, operation=operation), None,
                         images=images, area_m2=area, rooms=rooms, baths=baths, has_lift=prop["has_lift"])
        if normalized is not None:
            normalized.update({key: candidate[key] for key in ("url", "title", "description", "lang")})
            normalized["property"]["kind"] = property_kind
            candidate = dict(normalized, **dates)
    reasons = list(dict.fromkeys(reasons))
    return {"status": "pending" if reasons else "ready", "candidate": candidate, "reasons": reasons,
            "evidence": {"geography": geo_evidence, "dates": date_evidence, "source_pages": pages,
                         "image_scope": "observed_card_images", "gallery_complete": None}, "source": source}


def adapt_observed_cards(records, geography: MunicipalityIndex, *, as_of=None):
    """Partition every input once; reason counts may overlap for one input."""
    ready, pending = [], []
    for record in records:
        result = adapt_observed_card(record, geography, as_of=as_of)
        (ready if result["status"] == "ready" else pending).append(result)
    reasons = Counter(reason for result in pending for reason in result["reasons"])
    return {"ready": ready, "pending": pending,
            "counts": {"input": len(ready) + len(pending), "ready": len(ready), "pending": len(pending),
                       "by_reason": dict(sorted(reasons.items()))}}
