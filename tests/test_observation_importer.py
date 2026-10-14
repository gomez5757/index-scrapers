from copy import deepcopy
from pathlib import Path

import pytest

from index_scrapers.dedup import assign_property
from index_scrapers.observation_importer import import_observations, plan_observations
from index_scrapers.observed_idealista import adapt_observed_card
from index_scrapers.state import State
from index_scrapers.target_discovery import MunicipalityIndex


CAPTURED = "2026-10-14T13:50:43.771Z"
OLD = "2026-10-12T12:00:00Z"
IMAGE = "https://img4.idealista.com/blur/480_360_mq/card.webp"


@pytest.fixture
def envelope(listing):
    candidate = deepcopy(listing)
    candidate.pop("property_id")
    candidate.update(first_seen=CAPTURED, last_seen=CAPTURED, last_checked=CAPTURED)
    origin = {"observed_at": CAPTURED, "source_page": "https://www.idealista.com/alquiler-viviendas/madrid/"}
    return {"status": "ready", "candidate": candidate, "reasons": [],
            "evidence": {"gallery_complete": None, "image_scope": "observed_card_images"},
            "source": {"observed_at": CAPTURED, "source_card": {"price_text": "1.250 €/mes"},
                       "field_origins": {field: deepcopy(origin) for field in ("title", "url", "price_text", "details", "images")}}}


def result(report):
    return report["results"][0]


@pytest.mark.parametrize("operation,price", [("rent", 1250), ("sale", 250000)])
def test_preview_is_pure_and_explicit_import_is_stable(tmp_path, envelope, operation, price):
    envelope["candidate"]["property"]["operation"] = operation
    envelope["candidate"]["price"] = price
    before = deepcopy(envelope)
    with State(tmp_path / "state.sqlite") as state:
        preview = import_observations(state, [envelope])
        assert result(preview)["outcome"] == "planned"
        assert state.catalog() == []
        assert state.pending_count() == 0
        imported = import_observations(state, [envelope], apply=True)
        assert imported["counts"]["staged"] == 1
        saved = state.catalog()[0]
        assert saved["property_id"] == assign_property(envelope["candidate"], [])
        assert saved["first_seen"] == saved["last_seen"] == saved["last_checked"] == CAPTURED
        assert state.pending()[0]["listing"] == saved
        repeated = import_observations(state, [envelope], apply=True)
        assert repeated["counts"]["unchanged"] == 1
        assert state.pending_count() == 1
        assert state.catalog() == [saved]
    assert envelope == before
    assert result(imported)["envelope"] == before


def test_pending_invalid_and_conflicts_are_retained_without_writes(tmp_path, envelope, listing):
    pending = deepcopy(envelope)
    pending.update(status="pending", reasons=["first_seen_unknown"])
    pending["candidate"]["first_seen"] = None
    conflict = deepcopy(envelope)
    conflict["candidate"]["property"]["operation"] = "sale"
    with State(tmp_path / "state.sqlite") as state:
        state.observe(listing)
        before = state.catalog()
        count = state.pending_count()
        report = import_observations(state, [pending, None, conflict], apply=True)
        assert report["counts"]["input"] == 3
        assert report["counts"]["pending"] == 2
        assert report["counts"]["rejected"] == 1
        assert report["counts"]["by_reason"]["first_seen_unknown"] == 1
        assert report["results"][0]["envelope"] == pending
        assert report["results"][1]["envelope"] is None
        assert "immutable_property_conflict:operation" in report["results"][2]["gate_reasons"]
        import_observations(state, [pending, None, conflict], apply=True)
        assert state.catalog() == before
        assert state.pending_count() == count


@pytest.mark.parametrize("withdrawn", [False, True])
def test_old_capture_never_regresses_dates_or_reactivates(tmp_path, envelope, listing, withdrawn):
    listing.update(last_seen="2026-10-15T09:00:00Z", last_checked="2026-10-15T10:00:00Z")
    if withdrawn:
        listing["status"] = "removed"
    with State(tmp_path / "state.sqlite") as state:
        state.observe(listing)
        report = import_observations(state, [envelope], apply=True)
        assert result(report)["outcome"] == "ignored"
        assert result(report)["gate_reasons"] == ["stale_capture"]
        assert state.catalog() == [listing]
        assert state.pending_count() == 1


def test_newer_capture_preserves_identity_optional_values_and_full_gallery(tmp_path, envelope, listing):
    gallery = ["https://img4.idealista.com/full/a.jpg", "https://img4.idealista.com/full/b.jpg"]
    listing.update(images=gallery, advertiser="Agencia", photo_hash="0123456789abcdef")
    listing["property"].update(cover_image=gallery[0], has_parking=True, furnished=True)
    candidate = envelope["candidate"]
    candidate.update(description=None, lang=None, advertiser=None, images=[IMAGE])
    candidate["property"].update(district=None, baths=None, has_parking=None, furnished=None,
                                 cover_image=IMAGE, area_m2=85, rooms=3, has_lift=False)
    candidate["price"] = 1150
    with State(tmp_path / "state.sqlite") as state:
        state.observe(listing)
        report = import_observations(state, [envelope], apply=True)
        assert result(report)["outcome"] == "staged"
        saved = state.catalog()[0]
        assert saved["property_id"] == listing["property_id"]
        assert saved["first_seen"] == OLD
        assert saved["last_seen"] == saved["last_checked"] == CAPTURED
        for field in ("description", "lang", "advertiser", "images", "photo_hash"):
            assert saved[field] == listing[field]
        for field in ("district", "baths", "has_parking", "furnished", "cover_image"):
            assert saved["property"][field] == listing["property"][field]
        assert saved["price"] == 1150
        assert saved["property"]["rooms"] == 3
        assert saved["property"]["area_m2"] == 85
        assert saved["property"]["has_lift"] is False
        import_observations(state, [envelope], apply=True)
        assert state.pending_count() == 2


@pytest.mark.parametrize("field,value", [("province", "45"), ("municipality", "45168"), ("city_slug", "toledo")])
def test_immutable_geography_cannot_move_known_ad(tmp_path, envelope, listing, field, value):
    envelope["candidate"]["property"][field] = value
    with State(tmp_path / "state.sqlite") as state:
        state.observe(listing)
        report = import_observations(state, [envelope], apply=True)
        assert result(report)["outcome"] == "rejected"
        assert f"immutable_property_conflict:{field}" in result(report)["gate_reasons"]
        assert state.catalog() == [listing]


def test_conflicting_same_capture_is_pending_and_identical_replay_is_unchanged(tmp_path, envelope, listing):
    listing.update(last_seen=CAPTURED, last_checked=CAPTURED)
    envelope["candidate"]["price"] = 1000
    with State(tmp_path / "state.sqlite") as state:
        state.observe(listing)
        report = import_observations(state, [envelope], apply=True)
        assert result(report)["outcome"] == "pending"
        assert "conflicting_same_capture" in result(report)["gate_reasons"]
        envelope["candidate"]["price"] = listing["price"]
        assert result(import_observations(state, [envelope], apply=True))["outcome"] == "unchanged"
        assert state.catalog() == [listing]
        assert state.pending_count() == 1


def test_new_capture_cannot_replay_price_from_older_field_origin(tmp_path, envelope, listing):
    envelope["candidate"]["price"] = 1000
    envelope["source"]["field_origins"]["price_text"]["observed_at"] = "2026-10-11T00:00:00Z"
    with State(tmp_path / "state.sqlite") as state:
        state.observe(listing)
        report = import_observations(state, [envelope], apply=True)
        assert result(report)["outcome"] == "pending"
        assert "stale_field_evidence:price" in result(report)["gate_reasons"]
        assert state.catalog() == [listing]


@pytest.mark.parametrize("mutate,reason", [
    (lambda e: e["candidate"].update(status="removed"), "unobserved_status"),
    (lambda e: e["source"].update(observed_at=OLD), "capture_date_conflict"),
    (lambda e: e["candidate"].update(first_seen=None), "invalid_candidate"),
    (lambda e: e["candidate"]["property"].update(rooms=True), "invalid_candidate"),
    (lambda e: e["candidate"].update(property_id="01ARZ3NDEKTSV4RRFFQ69G5FAV"), "unexpected_property_id"),
])
def test_ready_label_does_not_bypass_input_gate(tmp_path, envelope, mutate, reason):
    mutate(envelope)
    with State(tmp_path / "state.sqlite") as state:
        report = import_observations(state, [envelope], apply=True)
        assert reason in result(report)["gate_reasons"]
        assert state.catalog() == []
        assert state.pending_count() == 0


def test_new_duplicate_uses_current_dedup_and_checks_property_group(envelope, listing):
    listing.update(id="fotocasa:20001", portal="fotocasa", photo_hash="0123456789abcdef")
    listing["property"]["cover_image"] = IMAGE
    envelope["candidate"]["property"]["cover_image"] = IMAGE
    envelope["candidate"]["photo_hash"] = listing["photo_hash"]
    report = plan_observations([envelope, deepcopy(envelope)], [listing])
    assert report["counts"]["planned"] == 1
    assert report["counts"]["unchanged"] == 1
    assert result(report)["candidate"]["property_id"] == listing["property_id"]
    listing["property"]["city_slug"] = "different-madrid-slug"
    report = plan_observations([envelope], [listing])
    assert result(report)["outcome"] == "rejected"
    assert "immutable_property_conflict:city_slug" in result(report)["gate_reasons"]


def test_property_group_preserves_richer_values_and_refuses_older_mutations(envelope, listing):
    sibling = deepcopy(listing)
    sibling.update(id="fotocasa:20001", portal="fotocasa", last_seen="2026-10-15T00:00:00Z", last_checked="2026-10-15T00:00:00Z")
    sibling["property"]["rooms"] = 4
    candidate = envelope["candidate"]
    candidate["property"]["rooms"] = None
    report = plan_observations([envelope], [listing, sibling])
    assert result(report)["candidate"]["property"]["rooms"] == 4
    candidate["property"]["rooms"] = 3
    report = plan_observations([envelope], [listing, sibling])
    assert result(report)["outcome"] == "pending"
    assert "stale_field_evidence:property.rooms" in result(report)["gate_reasons"]


def test_dedup_nearby_coordinates_keep_known_property_geography(envelope, listing):
    listing.update(id="fotocasa:20001", portal="fotocasa", photo_hash="0123456789abcdef")
    listing["property"].update(lat=40.4, lng=-3.7, cover_image=IMAGE)
    candidate = envelope["candidate"]
    candidate["photo_hash"] = listing["photo_hash"]
    candidate["property"].update(lat=40.4001, lng=-3.7, district="Nearby observed district", cover_image=IMAGE)
    report = plan_observations([envelope], [listing])
    assert result(report)["outcome"] == "planned"
    saved = result(report)["candidate"]
    assert saved["property_id"] == listing["property_id"]
    for field in ("district", "lat", "lng"):
        assert saved["property"][field] == listing["property"][field]


def test_unknown_optional_cannot_be_enriched_from_older_capture(envelope, listing):
    listing["property"]["has_lift"] = None
    envelope["candidate"]["property"]["has_lift"] = False
    envelope["source"]["field_origins"]["details"]["observed_at"] = "2026-10-11T00:00:00Z"
    report = plan_observations([envelope], [listing])
    assert result(report)["outcome"] == "pending"
    assert "stale_field_evidence:property.has_lift" in result(report)["gate_reasons"]


def test_freshness_only_import_updates_dates_without_an_extra_queue_entry(tmp_path, envelope, listing):
    with State(tmp_path / "state.sqlite") as state:
        state.observe(listing)
        report = import_observations(state, [envelope], apply=True)
        assert result(report)["outcome"] == "unchanged"
        assert state.catalog()[0]["last_checked"] == CAPTURED
        assert state.pending_count() == 1


def test_future_field_origin_does_not_bypass_capture_time(tmp_path, envelope):
    envelope["source"]["field_origins"]["price_text"]["observed_at"] = "2026-10-15T00:00:00Z"
    with State(tmp_path / "state.sqlite") as state:
        report = import_observations(state, [envelope], apply=True)
        assert result(report)["outcome"] == "pending"
        assert "future_field_evidence:price_text" in result(report)["gate_reasons"]
        assert state.catalog() == []


def test_application_requires_boolean_flag(tmp_path, envelope):
    with State(tmp_path / "state.sqlite") as state:
        with pytest.raises(ValueError, match="explicit boolean"):
            import_observations(state, [envelope], apply="false")
        assert state.catalog() == []


def test_newer_observed_presence_can_reactivate_removed_ad(tmp_path, envelope, listing):
    with State(tmp_path / "state.sqlite") as state:
        state.observe(listing)
        state.check(listing["id"], 404, "2026-10-12T13:00:00Z")
        assert state.check(listing["id"], 410, "2026-10-12T14:00:00Z") is True
        report = import_observations(state, [envelope], apply=True)
        assert result(report)["outcome"] == "staged"
        assert state.catalog()[0]["status"] == "active"
        assert state.catalog()[0]["property_id"] == listing["property_id"]
        assert state.catalog()[0]["first_seen"] == OLD
        assert state.pending_count() == 3


def test_equal_capture_in_another_timezone_does_not_rewrite_known_dates(tmp_path, envelope, listing):
    listing.update(last_seen=CAPTURED, last_checked=CAPTURED)
    equivalent = "2026-10-14T15:50:43.771+02:00"
    envelope["candidate"].update(first_seen=equivalent, last_seen=equivalent, last_checked=equivalent)
    envelope["source"]["observed_at"] = equivalent
    with State(tmp_path / "state.sqlite") as state:
        state.observe(listing)
        assert result(import_observations(state, [envelope], apply=True))["outcome"] == "unchanged"
        assert state.catalog() == [listing]
        assert state.pending_count() == 1


@pytest.mark.parametrize("operation,price_text", [("alquiler", "1.100 €/mes"), ("venta", "250.000 €")])
def test_real_adapter_envelope_can_pass_local_gate(tmp_path, operation, price_text):
    root = Path(__file__).resolve().parents[1]
    geography = MunicipalityIndex.from_csv(root / "data/municipalities.csv",
                                           provinces_html=root / "tests/fixtures/targets/ine-provinces.directory.html")
    source = {"id": "123456789", "observed_at": CAPTURED,
              "origins": [{"observed_at": CAPTURED, "source_page": f"https://www.idealista.com/{operation}-viviendas/madrid/"}],
              "source_card": {"id": "123456789", "url": "https://www.idealista.com/inmueble/123456789/",
                              "title": "Piso en Calle Mayor, Madrid", "price_text": price_text,
                              "details": ["80 m²", "2 hab.", "sin ascensor"], "images": [IMAGE]}}
    envelope = adapt_observed_card(source, geography)
    assert envelope["status"] == "ready"
    with State(tmp_path / "state.sqlite") as state:
        report = import_observations(state, [envelope], apply=True)
        assert report["counts"]["staged"] == 1
        assert state.catalog()[0]["last_checked"] == CAPTURED
        assert state.catalog()[0]["property"]["has_lift"] is False
        assert result(report)["envelope"]["source"] == source


def test_merge_cannot_change_the_identity_stage_will_assign(tmp_path, envelope, listing):
    first = deepcopy(listing)
    first.update(id="fotocasa:20001", portal="fotocasa", photo_hash="0123456789abcdef")
    first["property"].update(lat=40.4, lng=-3.7, cover_image=IMAGE)
    second = deepcopy(first)
    second.update(id="pisoscom:30001", portal="pisoscom",
                  last_seen="2026-10-13T12:00:00Z", last_checked="2026-10-13T12:00:00Z")
    second["property"]["lat"] = 40.4012
    candidate = envelope["candidate"]
    candidate["photo_hash"] = first["photo_hash"]
    candidate["property"].update(lat=40.4006, lng=-3.7, cover_image=IMAGE)
    assert assign_property(candidate, [first, second]) == listing["property_id"]
    shifted = deepcopy(candidate)
    shifted["property"]["lat"] = second["property"]["lat"]
    assert assign_property(shifted, [first, second]) != listing["property_id"]
    before = deepcopy(envelope)
    with State(tmp_path / "state.sqlite") as state:
        state.observe(first)
        state.observe(second)
        catalog = state.catalog()
        queued = state.pending_count()
        preview = import_observations(state, [envelope])
        imported = import_observations(state, [envelope], apply=True)
        for report in (preview, imported):
            assert result(report)["outcome"] == "pending"
            assert "merged_identity_conflict" in result(report)["gate_reasons"]
            assert result(report)["envelope"] == before
        assert state.catalog() == catalog
        assert state.pending_count() == queued


@pytest.mark.parametrize("defect", ["floor", "property_id"])
def test_merged_catalogue_values_must_match_delivery_schema(tmp_path, envelope, listing, defect):
    if defect == "floor":
        listing["property"]["floor"] = 1
    else:
        listing["property_id"] = "invalid-ulid"
    before = deepcopy(envelope)
    with State(tmp_path / "state.sqlite") as state:
        state.observe(listing)
        catalog = state.catalog()
        for apply in (False, True):
            report = import_observations(state, [envelope], apply=apply)
            assert result(report)["outcome"] == "pending"
            assert "invalid_merged_candidate" in result(report)["gate_reasons"]
            assert result(report)["envelope"] == before
        assert state.catalog() == catalog
        assert state.pending_count() == 1


@pytest.mark.parametrize("donated_hash", [None, "fedcba9876543210"])
def test_hash_reuse_always_belongs_to_the_merged_cover(tmp_path, envelope, listing, donated_hash):
    cover_a = "https://img4.idealista.com/full/a.jpg"
    cover_b = "https://img4.idealista.com/full/b.jpg"
    hash_a = "0123456789abcdef"
    listing.update(images=[cover_a], photo_hash=hash_a)
    listing["property"]["cover_image"] = cover_a
    sibling = deepcopy(listing)
    sibling.update(id="fotocasa:20001", portal="fotocasa", photo_hash=donated_hash,
                   last_seen="2026-10-13T12:00:00Z", last_checked="2026-10-13T12:00:00Z")
    sibling["property"]["cover_image"] = cover_b
    envelope["candidate"]["property"]["cover_image"] = cover_a
    envelope["candidate"]["photo_hash"] = hash_a
    before = deepcopy(envelope)
    with State(tmp_path / "state.sqlite") as state:
        state.observe(listing)
        state.observe(sibling)
        preview = result(import_observations(state, [envelope]))
        assert preview["outcome"] == "planned"
        planned = preview["candidate"]
        assert planned["property"]["cover_image"] == cover_b
        assert planned.get("photo_hash") == donated_hash
        assert planned.get("photo_hash") != hash_a
        imported = result(import_observations(state, [envelope], apply=True))
        assert imported["candidate"] == planned
        saved = next(record for record in state.catalog() if record["id"] == listing["id"])
        assert saved == planned
        queued = state.pending_count()
        assert queued == 3
        assert result(import_observations(state, [envelope], apply=True))["outcome"] == "unchanged"
        assert state.pending_count() == queued
        assert imported["envelope"] == before
        assert envelope == before


@pytest.mark.parametrize("known", [False, True])
def test_explicitly_observed_zero_price_matches_delivery_contract(tmp_path, envelope, listing, known):
    envelope["candidate"]["price"] = 0
    envelope["source"]["source_card"]["price_text"] = "0 €/mes"
    envelope["source"]["price_eur"] = 0
    before = deepcopy(envelope)
    with State(tmp_path / "state.sqlite") as state:
        if known:
            state.observe(listing)
        preview = result(import_observations(state, [envelope]))
        assert preview["outcome"] == "planned"
        assert preview["candidate"]["price"] == 0
        imported = result(import_observations(state, [envelope], apply=True))
        assert imported["outcome"] == "staged"
        assert state.catalog()[0]["price"] == 0
        assert state.pending_count() == 1 + int(known)
        assert imported["envelope"] == before
        assert envelope == before
        assert result(import_observations(state, [envelope], apply=True))["outcome"] == "unchanged"
        assert state.pending_count() == 1 + int(known)
