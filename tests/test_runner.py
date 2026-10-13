from copy import deepcopy
from pathlib import Path
import httpx
import pytest

CONTEXT = {"portal": "idealista", "operation": "rent", "province": "28", "municipality": "28079", "city_slug": "madrid", "url": "https://www.idealista.com/alquiler-viviendas/madrid-madrid/", "priority": "major"}


def test_priority_rent_before_sale_major_before_capitals_before_rest():
    from index_scrapers.runner import prioritize
    targets = [{**CONTEXT, "operation": "sale"}, {**CONTEXT, "municipality": "01001", "priority": "rest"}, {**CONTEXT, "municipality": "01059", "priority": "capital"}, CONTEXT]
    assert [(item["operation"], item["municipality"]) for item in prioritize(targets)] == [("rent", "28079"), ("rent", "01059"), ("rent", "01001"), ("sale", "28079")]


def test_discovery_reuses_persistent_identity_and_only_queues_changes(tmp_path):
    from index_scrapers.runner import discover
    from index_scrapers.state import State
    html = (Path(__file__).parent / "fixtures/idealista.synthetic.html").read_text(encoding="utf-8")
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=html))) as client, State(tmp_path / "state.sqlite") as state:
        report = discover(state, client, [CONTEXT], "2026-10-12T13:00:00Z", max_pages=1, hash_images=False)
        assert report["results"] == 1
        original = state.catalog()[0]["property_id"]
        state.acknowledge(state.pending(), ["idealista:10001"])
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=html))) as client, State(tmp_path / "state.sqlite") as state:
        discover(state, client, [CONTEXT], "2026-10-12T14:00:00Z", max_pages=1, hash_images=False)
        assert state.catalog()[0]["property_id"] == original
        assert state.pending() == []


def test_discovery_compares_dedup_against_complete_cross_portal_catalog(tmp_path, listing):
    from index_scrapers.runner import stage
    from index_scrapers.state import State
    first = deepcopy(listing)
    first["photo_hash"] = "0123456789abcdef"
    candidate = deepcopy(first)
    candidate.pop("property_id")
    candidate["id"] = "fotocasa:2"
    candidate["portal"] = "fotocasa"
    with State(tmp_path / "state.sqlite") as state:
        state.observe(first)
        stage(state, candidate, "2026-10-12T14:00:00Z")
        assert next(item for item in state.catalog() if item["id"] == "fotocasa:2")["property_id"] == first["property_id"]


def test_http_block_is_health_failure_without_removing_existing_listing(tmp_path, listing):
    from index_scrapers.runner import discover
    from index_scrapers.state import State
    with State(tmp_path / "state.sqlite") as state, httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client:
        state.observe(listing)
        report = discover(state, client, [CONTEXT], "2026-10-12T13:00:00Z", hash_images=False)
        assert report["anomalies"][0]["reason"] == "http_403"
        assert state.catalog()[0]["status"] == "active"


def test_zero_results_is_health_anomaly(tmp_path):
    from index_scrapers.runner import discover
    from index_scrapers.state import State
    with State(tmp_path / "state.sqlite") as state, httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text="<html></html>"))) as client:
        report = discover(state, client, [CONTEXT], "2026-10-12T13:00:00Z", hash_images=False)
        assert report["anomalies"][0]["reason"] == "zero_results"


def test_review_http_404_streak_survives_runner_restart(tmp_path, listing):
    from index_scrapers.runner import review
    from index_scrapers.state import State
    path = tmp_path / "state.sqlite"
    with State(path) as state, httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404))) as client:
        state.observe(listing)
        review(state, client, "rent", "2026-10-12T13:00:00Z", mode="withdrawals")
    with State(path) as state, httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(410))) as client:
        review(state, client, "rent", "2026-10-12T14:00:00Z", mode="withdrawals")
        assert state.catalog()[0]["status"] == "removed"


def test_discovery_follows_real_next_link_with_same_portal_only(tmp_path):
    from index_scrapers.runner import discover
    from index_scrapers.state import State
    html = (Path(__file__).parent / "fixtures/idealista.synthetic.html").read_text(encoding="utf-8")
    first = html + '<a rel="next" href="/alquiler-viviendas/madrid-madrid/pagina-2.htm">Siguiente</a>'
    def endpoint(request):
        return httpx.Response(200, text=first if "pagina-2" not in request.url.path else html.replace("10001", "10003"))
    with State(tmp_path / "state.sqlite") as state, httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        report = discover(state, client, [CONTEXT], "2026-10-12T13:00:00Z", max_pages=2, hash_images=False)
        assert report["results"] == 2
        assert {item["id"] for item in state.catalog()} == {"idealista:10001", "idealista:10003"}


def test_target_validation_rejects_invented_ine_code(tmp_path):
    from index_scrapers.runner import load_targets
    import json
    target = {**CONTEXT, "municipality": "99999", "province": "99"}
    path = tmp_path / "targets.json"
    path.write_text(json.dumps([target]))
    with pytest.raises(ValueError):
        load_targets(path, Path(__file__).parents[1] / "data/municipalities.csv")


def test_review_changes_price_using_real_detail_capture(tmp_path, listing):
    from index_scrapers.runner import review
    from index_scrapers.state import State
    html = (Path(__file__).parent / "fixtures/captures/idealista.detail-browser-20261012.html").read_text(encoding="utf-8")
    listing["id"] = "idealista:101920558"
    listing["url"] = "https://www.idealista.com/inmueble/101920558/"
    with State(tmp_path / "state.sqlite") as state, httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=html))) as client:
        state.observe(listing)
        state.acknowledge(state.pending(), [listing["id"]])
        report = review(state, client, "rent", "2026-10-12T14:00:00Z")
        assert report["changed"] == 1
        assert state.pending()[0]["listing"]["price"] == 1000


def test_transient_photo_download_failure_preserves_hash_for_same_cover(tmp_path, listing):
    from index_scrapers.runner import stage
    from index_scrapers.state import State
    listing["photo_hash"] = "0123456789abcdef"
    listing["property"]["cover_image"] = "https://img.idealista.com/a.jpg"
    with State(tmp_path / "state.sqlite") as state:
        state.observe(listing)
        updated = deepcopy(listing)
        updated["photo_hash"] = None
        stage(state, updated, "2026-10-12T14:00:00Z")
        assert state.catalog()[0]["photo_hash"] == "0123456789abcdef"


def test_unchanged_cover_does_not_redownload_image_on_each_discovery(tmp_path, listing):
    from index_scrapers.runner import discover
    from index_scrapers.state import State
    html = (Path(__file__).parent / "fixtures/idealista.synthetic.html").read_text(encoding="utf-8")
    photos = []
    def endpoint(request):
        if request.url.host == "img.idealista.com":
            photos.append(request.url)
            return httpx.Response(503)
        return httpx.Response(200, text=html)
    listing["property"]["cover_image"] = "https://img.idealista.com/fixture.jpg"
    listing["photo_hash"] = "0123456789abcdef"
    with State(tmp_path / "state.sqlite") as state, httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        state.observe(listing)
        discover(state, client, [CONTEXT], "2026-10-12T14:00:00Z")
        assert photos == []


def test_review_same_portal_redirect_cannot_change_other_listing_price(tmp_path, listing):
    from index_scrapers.runner import review
    from index_scrapers.state import State
    def endpoint(request):
        if request.url.path == "/inmueble/10001/":
            return httpx.Response(302, headers={"location": "https://www.idealista.com/inmueble/99999/"})
        return httpx.Response(200, text='<span class="info-data-price">777 €</span>')
    with State(tmp_path / "state.sqlite") as state, httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        state.observe(listing)
        state.acknowledge(state.pending(), [listing["id"]])
        report = review(state, client, "rent", "2026-10-12T14:00:00Z")
        assert state.catalog()[0]["price"] == 1250
        assert state.pending() == []
        assert report["anomalies"][0]["reason"] == "unexpected_listing_redirect"


def test_initial_load_resumes_next_page_from_persistent_cursor(tmp_path):
    from index_scrapers.runner import discover
    from index_scrapers.state import State
    html = (Path(__file__).parent / "fixtures/idealista.synthetic.html").read_text(encoding="utf-8")
    first = html + '<a rel="next" href="/alquiler-viviendas/madrid-madrid/pagina-2.htm">Siguiente</a>'
    visited = []
    def endpoint(request):
        visited.append(request.url.path)
        return httpx.Response(200, text=html.replace("10001", "10003") if "pagina-2" in request.url.path else first)
    with State(tmp_path / "state.sqlite") as state, httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        discover(state, client, [CONTEXT], "2026-10-12T13:00:00Z", max_pages=1, hash_images=False, resume=True)
    with State(tmp_path / "state.sqlite") as state, httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        discover(state, client, [CONTEXT], "2026-10-12T14:00:00Z", max_pages=1, hash_images=False, resume=True)
        assert visited == ["/alquiler-viviendas/madrid-madrid/", "/alquiler-viviendas/madrid-madrid/pagina-2.htm"]
        assert {row["id"] for row in state.catalog()} == {"idealista:10001", "idealista:10003"}


@pytest.mark.parametrize("portal,total,pages,size", [("fotocasa", 8522, 285, 30), ("habitaclia", 7654, 264, 29)])
def test_initial_pagination_metadata_matches_saved_portal_capture(portal, total, pages, size):
    from index_scrapers.runner import _pagination
    from selectolax.parser import HTMLParser
    html = (Path(__file__).parent / f"fixtures/captures/{portal}.http-200.html").read_text(encoding="utf-8")
    assert _pagination(HTMLParser(html), portal) == {"page": 1, "page_size": size, "total_results": total, "total_pages": pages, "has_more": True}
