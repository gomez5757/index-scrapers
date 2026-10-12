"""Tests use small invented records only; they are not the manual evaluation sample."""

from copy import deepcopy
from io import BytesIO
import importlib

import pytest


def listing(listing_id="idealista:1", **changes):
    item = {
        "id": listing_id,
        "price": 1000,
        "first_seen": "2026-10-12T12:00:00Z",
        "status": "active",
        "photo_hash": "aaaaaaaaaaaaaaaa",
        "property": {
            "operation": "rent",
            "municipality": "28079",
            "kind": "flat",
            "district": "Chamberí",
            "lat": 40.43,
            "lng": -3.70,
            "area_m2": 100,
            "rooms": 2,
        },
    }
    for key, value in changes.items():
        if key in item["property"]:
            item["property"][key] = value
        else:
            item[key] = value
    return item


def dedup():
    return importlib.import_module("index_scrapers.dedup")


def test_matching_records_from_two_portals_are_duplicates():
    assert dedup().is_duplicate(listing(), listing("fotocasa:2"))


@pytest.mark.parametrize(
    "changes",
    [
        {"operation": "sale"},
        {"municipality": "08019"},
        {"kind": "room"},
        {"rooms": 3},
        {"area_m2": 105.01},
        {"price": 1101},
        {"photo_hash": "5555555555555555"},
    ],
)
def test_a_failed_required_rule_keeps_records_separate(changes):
    assert not dedup().is_duplicate(listing(), listing("fotocasa:2", **changes))


@pytest.mark.parametrize("field,value", [("area_m2", 105), ("price", 1100)])
def test_area_and_price_tolerance_boundary_is_inclusive_and_symmetric(field, value):
    left, right = listing(), listing("fotocasa:2", **{field: value})
    assert dedup().is_duplicate(left, right)
    assert dedup().is_duplicate(right, left)


def test_distance_must_be_strictly_less_than_75_metres():
    # At the equator, these longitudes are 74.999 and 75.001 metres apart
    # for the documented spherical radius of 6,371,008.8 metres.
    left = listing(lat=0.0, lng=0.0)
    inside = listing("fotocasa:2", lat=0.0, lng=0.000674481280438)
    outside = listing("fotocasa:2", lat=0.0, lng=0.000674499267846)
    assert dedup().is_duplicate(left, inside)
    assert not dedup().is_duplicate(left, outside)


def test_district_is_normalized_when_either_record_lacks_coordinates():
    left = listing(lat=None, lng=None, district="  CHAMBERÍ  ")
    assert dedup().is_duplicate(left, listing("fotocasa:2", district="chamberi"))
    assert not dedup().is_duplicate(left, listing("fotocasa:2", district="Centro"))


@pytest.mark.parametrize(
    "changes",
    [
        {"area_m2": None}, {"area_m2": 0}, {"area_m2": float("nan")},
        {"rooms": None}, {"rooms": -1}, {"rooms": 2.5},
        {"price": 0}, {"price": float("inf")},
        {"photo_hash": None}, {"photo_hash": "invalid"},
        {"photo_hash": "0000000000000000"},
        {"photo_hash": "ffffffffffffffff"},
        {"district": None, "lat": None},
        {"lat": 999},
        {"municipality": None}, {"operation": None}, {"operation": []},
    ],
)
def test_missing_or_invalid_evidence_never_joins_records(changes):
    left, right = listing(), listing("fotocasa:2", **changes)
    assert not dedup().is_duplicate(left, right)
    assert not dedup().is_duplicate(right, left)


def test_studio_zero_rooms_is_known_information():
    assert dedup().is_duplicate(
        listing(kind="studio", rooms=0),
        listing("fotocasa:2", kind="studio", rooms=0),
    )


def test_distinct_ads_within_one_portal_are_kept_separate():
    assert not dedup().is_duplicate(listing(), listing("idealista:2"))


def test_hash_distance_boundary_uses_bits_not_hex_characters():
    assert dedup().is_duplicate(listing(), listing("fotocasa:2", photo_hash="aaaaaaaaaaaaaa95"))
    assert not dedup().is_duplicate(listing(), listing("fotocasa:2", photo_hash="aaaaaaaaaaaaaa15"))


def test_known_listing_keeps_its_property_id_after_data_changes():
    known = listing(property_id="01KNOWNPROPERTYID000000000")
    changed = listing(price=3000, area_m2=20, rooms=None, photo_hash=None)
    assert dedup().assign_property(changed, [known]) == "01KNOWNPROPERTYID000000000"


def test_new_listing_joins_existing_property_without_mutating_catalogue():
    known = listing(property_id="01KNOWNPROPERTYID000000000")
    candidate = listing("fotocasa:2")
    original = deepcopy([known, candidate])
    assert dedup().assign_property(candidate, [known]) == "01KNOWNPROPERTYID000000000"
    assert [known, candidate] == original


def test_matches_to_different_properties_are_left_ambiguous():
    left = listing(property_id="01LEFTPROPERTY000000000000")
    right = listing("pisoscom:3", property_id="01RIGHTPROPERTY00000000000")
    result = dedup().assign_property(listing("fotocasa:2"), [left, right])
    assert result not in {left["property_id"], right["property_id"]}


def test_pairwise_matching_does_not_create_a_transitive_merge():
    left = listing(lat=0.0, lng=0.0, property_id="01EXISTINGPROPERTY00000000")
    middle = listing("fotocasa:2", lat=0.0, lng=0.00045, property_id=left["property_id"])
    right = listing("pisoscom:3", lat=0.0, lng=0.00090)
    assert dedup().is_duplicate(left, middle)
    assert dedup().is_duplicate(middle, right)
    assert not dedup().is_duplicate(left, right)
    assert dedup().assign_property(right, [left, middle]) != left["property_id"]


def test_removed_listings_do_not_block_an_active_group_match():
    active = listing(property_id="01EXISTINGPROPERTY00000000")
    removed = listing("pisoscom:3", status="removed", rooms=8, property_id=active["property_id"])
    assert dedup().assign_property(listing("fotocasa:2"), [removed, active]) == active["property_id"]


def test_new_property_ids_are_reproducible_ulids_and_independent_of_catalogue_order():
    candidate = listing("fotocasa:2", rooms=None)
    catalog = [listing(property_id="old1"), listing("pisoscom:3", property_id="old2")]
    first = dedup().assign_property(candidate, catalog)
    assert first == dedup().assign_property(deepcopy(candidate), list(reversed(catalog)))
    assert len(first) == 26
    assert first[0] in "01234567"
    assert set(first) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
    assert first != dedup().assign_property(listing("fotocasa:3", rooms=None), [])


def test_conflicting_catalogue_ids_raise_instead_of_choosing_by_order():
    left = listing(property_id="first")
    right = listing(property_id="second")
    with pytest.raises(ValueError, match="conflict"):
        dedup().assign_property(listing(), [left, right])


@pytest.mark.parametrize("changes", [{"id": ""}, {"first_seen": "invalid"}])
def test_new_ids_require_a_stable_identity_and_valid_first_seen(changes):
    with pytest.raises(ValueError):
        dedup().assign_property(listing(**changes), [])


def test_missing_identity_cannot_match_an_incomplete_catalogue_record():
    with pytest.raises(ValueError):
        dedup().assign_property({"first_seen": "2026-10-12T12:00:00Z"}, [{"property_id": "unrelated"}])


def test_perceptual_hash_reads_bytes_without_creating_files(tmp_path, monkeypatch):
    image_module = pytest.importorskip("PIL.Image")
    picture = image_module.new("L", (9, 8))
    picture.putdata([255 if x % 2 == 0 else 0 for y in range(8) for x in range(9)])
    buffer = BytesIO()
    picture.save(buffer, format="PNG")
    monkeypatch.chdir(tmp_path)
    assert dedup().perceptual_hash(buffer.getvalue()) == "aaaaaaaaaaaaaaaa"
    assert list(tmp_path.iterdir()) == []


def test_invalid_image_data_is_rejected():
    pytest.importorskip("PIL.Image")
    with pytest.raises(ValueError, match="image"):
        dedup().perceptual_hash(b"not an image")
