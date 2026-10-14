from copy import deepcopy
import json
from pathlib import Path

import pytest

from index_scrapers.extractors import row
from index_scrapers.observed_idealista import adapt_observed_card, adapt_observed_cards
from index_scrapers.runner import stage
from index_scrapers.state import State
from index_scrapers.target_discovery import MunicipalityIndex


ROOT = Path(__file__).resolve().parents[1]
CAPTURED = "2026-10-14T13:50:43.771Z"
EARLIER = "2026-10-13T09:00:00+02:00"
LISTING = "https://www.idealista.com/inmueble/112800963/"
IMAGE = "https://img4.idealista.com/blur/480_360_mq/example.webp"


@pytest.fixture(scope="module")
def geography():
    return MunicipalityIndex.from_csv(
        ROOT / "data/municipalities.csv",
        provinces_html=ROOT / "tests/fixtures/targets/ine-provinces.directory.html",
    )


@pytest.fixture
def observed():
    origin = {"observed_at": CAPTURED, "source_page": "https://www.idealista.com/alquiler-viviendas/madrid-provincia/",
              "file": "saved/chunk.json", "sha256": "a" * 64}
    return {"id": "112800963", "observed_at": CAPTURED, "origins": [origin], "price_eur": 1100,
            "area_m2": 80, "rooms": 2, "bathrooms": None, "published_at": None,
            "field_origins": {name: deepcopy(origin) for name in ("title", "url", "price_text", "details", "images")},
            "source_card": {"id": "112800963", "url": LISTING,
                            "title": "Piso en Calle de Benito Gutiérrez, 14, Argüelles, Madrid",
                            "price_text": "1.100€/mes", "details": ["2 hab.", "80 m²", "Bajo interior con ascensor", "Publicado ayer"],
                            "images": [IMAGE]}}


def test_rent_uses_only_observed_text_and_has_no_provisional_identity(observed, geography):
    before = deepcopy(observed)
    result = adapt_observed_card(observed, geography)
    assert result["status"] == "ready"
    candidate = result["candidate"]
    assert candidate["id"] == "idealista:112800963"
    assert candidate["url"] == LISTING
    assert candidate["price"] == 1100
    assert candidate["property"]["operation"] == "rent"
    assert candidate["property"]["kind"] == "flat"
    assert candidate["property"]["municipality"] == "28079"
    assert candidate["property"]["area_m2"] == 80
    assert candidate["property"]["rooms"] == 2
    assert candidate["property"]["has_lift"] is True
    assert candidate["description"] is candidate["lang"] is candidate["advertiser"] is None
    assert candidate["property"]["district"] is candidate["property"]["baths"] is None
    assert not {"property_id", "photo_hash", "published_at"} & candidate.keys()
    assert result["evidence"]["gallery_complete"] is None
    assert result["evidence"]["image_scope"] == "observed_card_images"
    assert result["evidence"]["dates"]["published_at"] is None
    assert observed == before == result["source"]
    result["source"]["source_card"]["title"] = "changed copy"
    assert observed == before


def test_sale_operation_comes_from_observed_page(observed, geography):
    for origin in observed["origins"] + list(observed["field_origins"].values()):
        origin["source_page"] = "https://www.idealista.com/venta-viviendas/madrid-provincia/pagina-2.htm"
    observed["source_card"]["price_text"] = "250.000 €"
    observed["price_eur"] = 250000
    result = adapt_observed_card(observed, geography)
    assert result["status"] == "ready"
    assert result["candidate"]["property"]["operation"] == "sale"
    assert result["candidate"]["price"] == 250000


@pytest.mark.parametrize("title,municipality,province", [
    ("Piso en Calle Francisco de Goya, 22, El Quiñón, Seseña", "45161", "45"),
    ("Piso en Calle Nte., 10, Azucaica, Toledo", "45168", "45"),
    ("Chalet adosado en Avenida Camino del Pozo, 111, El Casar", "19071", "19"),
    ("Chalet adosado en Yuncos", "45205", "45"),
])
def test_observed_municipality_overrides_no_province_by_page(title, municipality, province, observed, geography):
    observed["source_card"]["title"] = title
    result = adapt_observed_card(observed, geography)
    assert result["status"] == "ready"
    assert result["candidate"]["property"]["municipality"] == municipality
    assert result["candidate"]["property"]["province"] == province


def test_ambiguous_municipality_stays_pending_without_using_page_province(observed, geography):
    observed["source_card"]["title"] = "Piso en Calle Mayor, Arroyomolinos"
    result = adapt_observed_card(observed, geography)
    assert result["status"] == "pending"
    assert "ambiguous_municipality" in result["reasons"]
    assert len(result["evidence"]["geography"]["candidates"]) > 1
    assert result["candidate"]["property"]["province"] is None


@pytest.mark.parametrize("title", ["Piso en La Moraleja", "Casa o chalet independiente en Ciudalcampo", "Piso en Calle Madrid"])
def test_unstructured_places_are_not_mapped_to_a_nearby_municipality(title, observed, geography):
    observed["source_card"]["title"] = title
    result = adapt_observed_card(observed, geography)
    assert result["status"] == "pending"
    assert "unknown_municipality" in result["reasons"]
    assert result["candidate"]["property"]["municipality"] is None


def test_kind_is_anchored_to_property_type_not_street_name(observed, geography):
    observed["source_card"]["title"] = "Piso en Calle Ático, Casa de Campo, Madrid"
    assert adapt_observed_card(observed, geography)["candidate"]["property"]["kind"] == "flat"
    observed["source_card"]["title"] = "Pide fotos al anunciante"
    assert "unknown_kind" in adapt_observed_card(observed, geography)["reasons"]


def test_missing_optional_assertions_remain_null_instead_of_using_aggregate_numbers(observed, geography):
    observed["source_card"]["details"] = None
    observed["source_card"]["images"] = None
    result = adapt_observed_card(observed, geography)
    assert result["status"] == "ready"
    prop = result["candidate"]["property"]
    assert prop["area_m2"] is prop["rooms"] is prop["baths"] is prop["has_lift"] is prop["cover_image"] is None
    assert result["candidate"]["images"] == []


def test_missing_price_text_cannot_use_aggregate_price(observed, geography):
    observed["source_card"]["price_text"] = None
    result = adapt_observed_card(observed, geography)
    assert result["status"] == "pending"
    assert "missing_price_text" in result["reasons"]
    assert result["candidate"]["price"] is None
    assert result["source"]["price_eur"] == 1100


def test_every_input_is_preserved_in_report(observed, geography):
    inputs = [observed, {"id": "123", "source_card": {"id": "123"}}, {}, None]
    report = adapt_observed_cards(inputs, geography)
    assert report["counts"]["input"] == 4
    assert report["counts"]["ready"] == 1
    assert report["counts"]["pending"] == 3
    assert report["counts"]["by_reason"]["missing_listing_url"] == 2
    assert report["pending"][-1]["source"] is None
    assert report["pending"][-1]["reasons"] == ["invalid_record"]
    assert json.loads(json.dumps(report))["counts"] == report["counts"]


@pytest.mark.parametrize("url", [
    "http://www.idealista.com/inmueble/112800963/",
    "https://www.idealista.com.evil.example/inmueble/112800963/",
    "https://user@www.idealista.com/inmueble/112800963/",
    "https://www.idealista.com:443/inmueble/112800963/",
    "https://www.idealista.com/inmueble/112800963/?tracking=1",
    "https://www.idealista.com/inmueble/112800963/#photos",
    "https://www.idealista.com/a/../inmueble/112800963/",
    "https://www.idealista.com/%2e%2e/inmueble/112800963/",
    "/inmueble/112800963/",
    "https://www.idealista.com/inmueble/112800963/\n",
])
def test_suspicious_listing_url_is_not_repaired(url, observed, geography):
    observed["source_card"]["url"] = url
    result = adapt_observed_card(observed, geography)
    assert "invalid_listing_url" in result["reasons"]
    assert result["candidate"]["url"] is None
    assert result["source"]["source_card"]["url"] == url


@pytest.mark.parametrize("page,reason", [
    ("https://www.idealista.com/", "unknown_operation"),
    ("https://www.idealista.com/ayuda/alquiler-viviendas/", "unknown_operation"),
    ("https://localhost/alquiler-viviendas/madrid-provincia/", "invalid_source_page"),
])
def test_unknown_or_untrusted_page_cannot_supply_operation(page, reason, observed, geography):
    for origin in observed["origins"] + list(observed["field_origins"].values()):
        origin["source_page"] = page
    result = adapt_observed_card(observed, geography)
    assert reason in result["reasons"]
    assert result["candidate"]["property"]["operation"] is None


def test_sale_page_prefix_wins_over_deeper_rent_substring(observed, geography):
    page = "https://www.idealista.com/venta-viviendas/madrid-provincia/alquiler-extra/"
    for origin in observed["origins"] + list(observed["field_origins"].values()):
        origin["source_page"] = page
    observed["source_card"]["price_text"] = "250.000 €"
    observed["price_eur"] = 250000
    result = adapt_observed_card(observed, geography)
    assert result["status"] == "ready"
    assert result["candidate"]["property"]["operation"] == "sale"
    assert result["evidence"]["source_pages"] == [page]


@pytest.mark.parametrize("length,expected_status", [(190, "ready"), (191, "pending")])
def test_full_listing_id_respects_api_length_without_truncating(length, expected_status, observed, geography):
    identifier = "1" * length
    observed["id"] = observed["source_card"]["id"] = identifier
    observed["source_card"]["url"] = "https://www.idealista.com/inmueble/" + identifier + "/"
    result = adapt_observed_card(observed, geography)
    assert result["status"] == expected_status
    assert ("listing_id_too_long" in result["reasons"]) == (expected_status == "pending")
    assert result["candidate"]["id"] == "idealista:" + identifier
    assert len(result["candidate"]["id"]) == (200 if expected_status == "ready" else 201)
    assert result["source"] == observed


def test_conflicting_identity_and_price_are_pending(observed, geography):
    observed["source_card"]["id"] = "999"
    observed["price_eur"] = 1200
    result = adapt_observed_card(observed, geography)
    assert {"conflicting_listing_id", "conflicting_price"} <= set(result["reasons"])


def test_conflicting_page_operations_and_price_period_are_pending(observed, geography):
    observed["field_origins"]["title"]["source_page"] = "https://www.idealista.com/venta-viviendas/madrid-provincia/"
    assert "conflicting_operation" in adapt_observed_card(observed, geography)["reasons"]
    observed["field_origins"]["title"]["source_page"] = observed["origins"][0]["source_page"]
    observed["source_card"]["price_text"] = "1.100 €"
    assert "price_operation_conflict" in adapt_observed_card(observed, geography)["reasons"]


@pytest.mark.parametrize("date", [None, "2026-10-14", "2026-10-14T13:50:43", "2026-02-30T13:50:43Z", "2026-10-14T13:50:43+00:99", "not-a-date"])
def test_missing_or_invalid_capture_time_stays_pending(date, observed, geography):
    observed["observed_at"] = date
    result = adapt_observed_card(observed, geography)
    assert ("missing_observed_at" if date is None else "invalid_observed_at") in result["reasons"]
    assert result["status"] == "pending"
    assert result["candidate"]["last_seen"] is None
    assert result["source"]["observed_at"] == date


def test_earliest_real_observation_uses_timezones_and_preserves_literal_date(observed, geography):
    observed["origins"].append({"observed_at": EARLIER, "source_page": observed["origins"][0]["source_page"]})
    result = adapt_observed_card(observed, geography)
    assert result["status"] == "ready"
    assert result["candidate"]["first_seen"] == EARLIER
    assert result["candidate"]["last_checked"] == CAPTURED


def test_undated_older_observation_does_not_invent_first_seen(observed, geography):
    observed["origins"].insert(0, {"observed_at": None, "source_page": observed["origins"][0]["source_page"]})
    result = adapt_observed_card(observed, geography)
    assert "first_seen_unknown" in result["reasons"]
    assert result["candidate"]["first_seen"] is None
    assert result["evidence"]["dates"]["earliest_known_at"] == CAPTURED
    assert result["evidence"]["dates"]["has_undated_observations"] is True


def test_future_and_inconsistent_observation_dates_are_pending(observed, geography):
    assert "future_observed_at" in adapt_observed_card(observed, geography, as_of="2026-10-14T00:00:00Z")["reasons"]
    observed["observed_at"] = EARLIER
    assert "observed_at_precedes_origin" in adapt_observed_card(observed, geography)["reasons"]
    with pytest.raises(ValueError, match="as_of"):
        adapt_observed_card(observed, geography, as_of="2026-10-14")


def test_observed_images_are_unique_trusted_urls_without_hashes(observed, geography):
    observed["source_card"]["images"] = [IMAGE, IMAGE, "https://evil.example/a.jpg"]
    result = adapt_observed_card(observed, geography)
    assert "invalid_image_url" in result["reasons"]
    assert result["candidate"]["images"] == [IMAGE]
    assert "photo_hash" not in result["candidate"]
    assert result["source"]["source_card"]["images"][-1] == "https://evil.example/a.jpg"


def test_conflicting_or_fractional_measurements_are_not_truncated(observed, geography):
    observed["source_card"]["details"] = ["80,5 m²", "2 hab.", "3 hab."]
    result = adapt_observed_card(observed, geography)
    assert {"invalid_or_conflicting_area_m2", "invalid_or_conflicting_rooms"} <= set(result["reasons"])
    assert result["candidate"]["property"]["area_m2"] is None
    assert result["candidate"]["property"]["rooms"] is None


def test_ready_candidate_matches_real_normalizer_and_stage_queue(observed, geography, tmp_path):
    result = adapt_observed_card(observed, geography)
    candidate = result["candidate"]
    normalized = row("idealista", observed["id"], LISTING, observed["source_card"]["title"], 1100,
                     {"operation": "rent", "province": "28", "municipality": "28079", "city_slug": "madrid"}, None,
                     images=[IMAGE], area_m2=80, rooms=2, has_lift=True)
    normalized["description"] = None
    assert candidate == dict(normalized, first_seen=CAPTURED, last_seen=CAPTURED, last_checked=CAPTURED)
    with State(tmp_path / "state.sqlite") as state:
        assert stage(state, candidate, candidate["last_checked"]) is True
        wire = state.pending()[0]["listing"]
        assert wire["first_seen"] == wire["last_seen"] == wire["last_checked"] == CAPTURED
        assert len(wire["property_id"]) == 26
        assert wire["property"]["municipality"] == "28079"
        assert wire["title"] == candidate["title"]
        assert wire["description"] is None
        assert stage(state, candidate, candidate["last_checked"]) is False
        assert state.pending_count() == 1
        property_id = wire["property_id"]
    with State(tmp_path / "another-state.sqlite") as state:
        stage(state, candidate, candidate["last_checked"])
        assert state.pending()[0]["listing"]["property_id"] == property_id
    assert "property_id" not in candidate
