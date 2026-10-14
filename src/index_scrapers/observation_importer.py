"""Local catalogue gate for saved Idealista observation envelopes.

Planning performs no I/O. ``import_observations`` only stages when ``apply=True``;
it never delivers to the API or interprets import time as observation time.
The caller must durably retain the original envelopes outside the delivery
schema before applying, and serialize writes to the shared State while importing.
"""

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
import re

from .dedup import assign_property
from .extractors import trusted_image_url
from .observed_idealista import _public_url, _timestamp
from .runner import stage
from .state import fingerprint


_LISTING_FIELDS = {
    "id", "portal", "url", "title", "description", "lang", "price", "images",
    "advertiser", "status", "first_seen", "last_seen", "last_checked", "property",
}
_PROPERTY_FIELDS = {
    "operation", "kind", "province", "municipality", "city_slug", "district",
    "lat", "lng", "area_m2", "rooms", "baths", "floor", "has_lift",
    "has_parking", "has_terrace", "furnished", "cover_image",
}
_IMMUTABLE = ("operation", "province", "municipality", "city_slug")
_PRESERVED_GEO = {"district", "lat", "lng"}
_OUTCOMES = ("planned", "staged", "unchanged", "pending", "ignored", "rejected")


def _text(value, maximum, nullable=False):
    return nullable and value is None or isinstance(value, str) and len(value) <= maximum


def _integer(value, maximum, nullable=False):
    return nullable and value is None or type(value) is int and 0 <= value <= maximum


def _valid_candidate(candidate):
    if not isinstance(candidate, Mapping) or not _LISTING_FIELDS <= candidate.keys():
        return False
    if not candidate.keys() <= _LISTING_FIELDS | {"photo_hash", "property_id"}:
        return False
    prop = candidate["property"]
    if not isinstance(prop, Mapping) or prop.keys() != _PROPERTY_FIELDS:
        return False
    identity = candidate["id"]
    if not isinstance(identity, str) or not re.fullmatch(r"idealista:\d{1,190}", identity):
        return False
    if "property_id" in candidate and (not isinstance(candidate["property_id"], str) or not re.fullmatch(
            r"[0-7][0-9A-HJKMNP-TV-Z]{25}", candidate["property_id"])):
        return False
    if candidate["portal"] != "idealista" or not _public_url(candidate["url"]):
        return False
    if not re.fullmatch(r"https://(?:www\.)?idealista\.com/inmueble/" + identity[10:] + r"/?", candidate["url"]):
        return False
    dates = [_timestamp(candidate[field]) for field in ("first_seen", "last_seen", "last_checked")]
    if any(value is None for value in dates) or not dates[0] <= dates[1] == dates[2]:
        return False
    if not all(_text(candidate[field], maximum, True) for field, maximum in
               (("title", 300), ("description", 20000), ("lang", 16), ("advertiser", 300))):
        return False
    if not _integer(candidate["price"], 1_000_000_000):
        return False
    images = candidate["images"]
    if not isinstance(images, list) or len(images) > 30 or not all(
            _text(image, 2048) and trusted_image_url(image) for image in images):
        return False
    if prop["operation"] not in ("rent", "sale") or prop["kind"] not in (
            "flat", "house", "studio", "penthouse", "duplex", "room", "other"):
        return False
    if not all(isinstance(prop[field], str) and re.fullmatch(pattern, prop[field])
               for field, pattern in (("province", r"\d{2}"), ("municipality", r"\d{5}"),
                                      ("city_slug", r"[a-z][a-z0-9-]{0,79}"))):
        return False
    if not all(_text(prop[field], maximum, True) for field, maximum in (("district", 120), ("floor", 80))):
        return False
    if not all(_integer(prop[field], maximum, True) for field, maximum in
               (("area_m2", 100000), ("rooms", 100), ("baths", 100))):
        return False
    if not all(prop[field] is None or type(prop[field]) is bool
               for field in ("has_lift", "has_parking", "has_terrace", "furnished")):
        return False
    for field, bound in (("lat", 90), ("lng", 180)):
        value = prop[field]
        if value is not None and (type(value) not in (int, float) or not -bound <= value <= bound):
            return False
    if (prop["lat"] is None) != (prop["lng"] is None):
        return False
    cover = prop["cover_image"]
    if cover is not None and not (_text(cover, 2048) and trusted_image_url(cover)):
        return False
    photo = candidate.get("photo_hash")
    return photo is None or cover is not None and isinstance(photo, str) and re.fullmatch(
        r"[0-9a-fA-F]{16}", photo) is not None


def _clock(record):
    values = [_timestamp(record.get(field)) for field in ("last_seen", "last_checked")]
    return max(values) if all(value is not None for value in values) else None


def _field_time(envelope, field):
    source = envelope["source"]
    origins = source.get("field_origins")
    if isinstance(origins, Mapping):
        origin = origins.get(field)
        return _timestamp(origin.get("observed_at")) if isinstance(origin, Mapping) else None
    return _timestamp(source.get("observed_at"))


def _evidence_conflict(envelope, origin, field, baseline):
    observed = _field_time(envelope, origin)
    if observed is None or observed <= baseline:
        return "stale_field_evidence:" + field
    if observed > _timestamp(envelope["candidate"]["last_checked"]):
        return "future_field_evidence:" + field
    return None


def _merged(candidate, existing, members):
    merged = deepcopy(candidate)
    if existing:
        merged["first_seen"] = existing["first_seen"]
        for field in ("title", "description", "lang", "advertiser"):
            if merged.get(field) is None and field in existing:
                merged[field] = deepcopy(existing[field])
        # Card thumbnails cannot establish that a previously known gallery changed.
        if existing.get("images"):
            merged["images"] = deepcopy(existing["images"])
    ordered = sorted(members, key=_clock, reverse=True)
    for field in _PROPERTY_FIELDS:
        donor = next((record for record in ordered if record["property"].get(field) is not None), None)
        if donor and (merged["property"][field] is None or field in _PRESERVED_GEO | {"cover_image"}):
            merged["property"][field] = deepcopy(donor["property"][field])
    # A hash is evidence for one cover URL, not for the property as a whole.
    cover = merged["property"]["cover_image"]
    photo_sources = [candidate] + ([existing] if existing else []) + ordered
    photo_source = next((record for record in photo_sources if cover is not None
                         and record["property"].get("cover_image") == cover
                         and record.get("photo_hash") is not None), None)
    merged.pop("photo_hash", None)
    if photo_source:
        merged["photo_hash"] = deepcopy(photo_source["photo_hash"])
    elif existing and cover == existing["property"].get("cover_image"):
        # Mirror stage's same-cover reuse so the preview matches the saved row.
        merged["photo_hash"] = existing.get("photo_hash")
    return merged


def _field_conflicts(envelope, merged, existing, members):
    reasons = []
    if existing:
        for field in ("price", "title", "url", "description", "lang", "advertiser"):
            if merged.get(field) != existing.get(field):
                origin = "price_text" if field == "price" else field
                conflict = _evidence_conflict(envelope, origin, field, _clock(existing))
                if conflict:
                    reasons.append(conflict)
    ordered = sorted(members, key=_clock, reverse=True)
    for field in _PROPERTY_FIELDS - set(_IMMUTABLE) - _PRESERVED_GEO - {"cover_image"}:
        donor = next((record for record in ordered if record["property"].get(field) is not None), None)
        if donor is None and ordered and merged["property"][field] is not None:
            donor = ordered[0]
        if donor and merged["property"][field] != donor["property"].get(field):
            conflict = _evidence_conflict(envelope, "title" if field == "kind" else "details",
                                          "property." + field, _clock(donor))
            if conflict:
                reasons.append(conflict)
    return sorted(reasons)


def _decision(envelope, catalog):
    result = {"envelope": deepcopy(envelope), "outcome": "pending", "gate_reasons": [],
              "candidate": None, "changed": False}

    def finish(outcome, *reasons):
        result.update(outcome=outcome, gate_reasons=list(reasons))
        return result

    if not isinstance(envelope, Mapping) or not isinstance(envelope.get("reasons"), list) or not all(
            isinstance(reason, str) for reason in envelope["reasons"]):
        return finish("pending", "invalid_envelope")
    if envelope.get("status") == "pending":
        return finish("pending")
    if envelope.get("status") != "ready" or envelope["reasons"] or not all(
            isinstance(envelope.get(field), Mapping) for field in ("source", "evidence")):
        return finish("pending", "invalid_envelope")
    candidate = envelope.get("candidate")
    if not _valid_candidate(candidate):
        return finish("pending", "invalid_candidate")
    if "property_id" in candidate:
        return finish("rejected", "unexpected_property_id")
    if candidate["status"] != "active":
        return finish("rejected", "unobserved_status")
    capture = _timestamp(candidate["last_checked"])
    if _timestamp(envelope["source"].get("observed_at")) != capture:
        return finish("pending", "capture_date_conflict")
    origins = envelope["source"].get("field_origins")
    if isinstance(origins, Mapping):
        for field in ("title", "url", "price_text", "details", "images"):
            origin = origins.get(field)
            observed = _timestamp(origin.get("observed_at")) if isinstance(origin, Mapping) else None
            if observed is not None and observed > capture:
                return finish("pending", "future_field_evidence:" + field)
    existing_records = [record for record in catalog if record.get("id") == candidate["id"]]
    if len(existing_records) > 1:
        return finish("pending", "catalog_listing_conflict")
    existing = existing_records[0] if existing_records else None
    if existing:
        if _clock(existing) is None or _timestamp(existing.get("first_seen")) is None:
            return finish("pending", "invalid_catalog_chronology")
        if capture < _clock(existing):
            return finish("ignored", "stale_capture")
    try:
        property_id = assign_property(candidate, catalog)
    except (ValueError, KeyError, TypeError):
        return finish("pending", "catalog_identity_conflict")
    members = [record for record in catalog if record.get("property_id") == property_id]
    if any(_clock(record) is None or not isinstance(record.get("property"), Mapping) for record in members):
        return finish("pending", "invalid_catalog_chronology")
    for field in _IMMUTABLE:
        values = [record["property"].get(field) for record in members if record["property"].get(field) is not None]
        if values and any(value != values[0] for value in values):
            return finish("pending", "catalog_property_conflict:" + field)
        incoming = candidate["property"][field]
        if values and incoming is not None and incoming != values[0]:
            return finish("rejected", "immutable_property_conflict:" + field)
    merged = _merged(candidate, existing, members)
    merged["property_id"] = property_id
    if not _valid_candidate(merged):
        return finish("pending", "invalid_merged_candidate")
    try:
        final_property_id = assign_property(merged, catalog)
    except (ValueError, KeyError, TypeError):
        return finish("pending", "catalog_identity_conflict")
    if final_property_id != property_id:
        return finish("pending", "merged_identity_conflict")
    changed = existing is None or fingerprint(merged) != fingerprint(existing)
    if existing and capture == _clock(existing):
        if changed:
            return finish("pending", "conflicting_same_capture")
        result["candidate"] = deepcopy(existing)
        return finish("unchanged")
    conflicts = _field_conflicts(envelope, merged, existing, members)
    if conflicts:
        return finish("pending", *conflicts)
    result.update(candidate=merged, changed=changed)
    return finish("planned")


def _report(results, applied):
    outcomes = Counter(result["outcome"] for result in results)
    reasons = Counter()
    for result in results:
        envelope = result["envelope"]
        original = envelope.get("reasons", []) if isinstance(envelope, Mapping) else []
        original = [reason for reason in original if isinstance(reason, str)] if isinstance(original, list) else []
        reasons.update(list(dict.fromkeys(original + result["gate_reasons"])))
    return {"applied": applied, "results": results,
            "counts": {"input": len(results), **{outcome: outcomes[outcome] for outcome in _OUTCOMES},
                       "by_reason": dict(sorted(reasons.items()))}}


def plan_observations(envelopes, catalog):
    """Review every envelope against a catalogue copy, including earlier planned ads."""
    current = deepcopy(list(catalog))
    results = []
    for envelope in envelopes:
        result = _decision(envelope, current)
        results.append(result)
        if result["outcome"] == "planned":
            current = [record for record in current if record["id"] != result["candidate"]["id"]]
            current.append(deepcopy(result["candidate"]))
    return _report(results, False)


def import_observations(state, envelopes, *, apply=False):
    """Preview by default; explicitly apply eligible captures to local State only.

    Preserve this report and its original envelopes separately from the queue.
    Pending, rejected and ignored observations are reported without State writes.
    Catalogue decisions are recomputed before every application, never replayed
    from an earlier plan. Only real captured timestamps are passed to ``stage``.
    """
    if type(apply) is not bool:
        raise ValueError("apply must be an explicit boolean")
    if not apply:
        return plan_observations(envelopes, state.catalog())
    results = []
    for envelope in envelopes:
        result = _decision(envelope, state.catalog())
        if result["outcome"] == "planned":
            result["changed"] = stage(state, result["candidate"], result["candidate"]["last_checked"])
            result["outcome"] = "staged" if result["changed"] else "unchanged"
        results.append(result)
    return _report(results, True)
