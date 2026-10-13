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


def test_withdrawal_recheck_waits_an_hour_without_repeating_the_sweep(tmp_path, listing):
    from index_scrapers.runner import review
    from index_scrapers.state import State
    path = tmp_path / "state.sqlite"
    visited = []
    def endpoint(request):
        visited.append(request.url.path)
        return httpx.Response(404)
    with State(path) as state, httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        state.observe(listing)
        state.acknowledge(state.pending(), [listing["id"]])
        review(state, client, "rent", "2026-10-12T13:00:00.123456Z", mode="withdrawals")
    with State(path) as state, httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        early = review(state, client, "rent", "2026-10-12T14:00:00.123455Z", mode="withdrawals")
        assert early["checked"] == 0
        assert state.pending() == []
        due = review(state, client, "rent", "2026-10-12T14:00:00.123456Z", mode="withdrawals")
        assert due["checked"] == due["changed"] == 1
        assert visited == ["/inmueble/10001/"] * 2


def test_withdrawal_cursor_resumes_independently_of_price_checks(tmp_path, listing):
    from index_scrapers.runner import review
    from index_scrapers.state import State
    path = tmp_path / "state.sqlite"
    visited = []
    def endpoint(request):
        visited.append(request.url.path)
        return httpx.Response(200, text='<span class="info-data-price">1250 €</span>')
    with State(path) as state, httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        state.observe(listing)
        second = deepcopy(listing)
        second["id"] = "idealista:10002"
        second["url"] = "https://www.idealista.com/inmueble/10002/"
        state.observe(second)
        first = review(state, client, "rent", "2026-10-12T13:00:00Z", mode="withdrawals", limit=1)
        assert first["withdrawals"]["sweep_remaining"] == 1
        review(state, client, "rent", "2026-10-12T13:30:00Z", mode="prices", limit=1)
    with State(path) as state, httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        resumed = review(state, client, "rent", "2026-10-12T14:00:00Z", mode="withdrawals", limit=1)
        assert resumed["withdrawals"]["sweep_remaining"] == 0
        assert visited == ["/inmueble/10001/", "/inmueble/10002/", "/inmueble/10002/"]
        assert review(state, client, "rent", "2026-10-12T15:00:00Z", mode="withdrawals")["checked"] == 0
        assert review(state, client, "rent", "2026-10-13T01:00:00Z", mode="withdrawals")["checked"] == 2


def test_due_rechecks_have_priority_respect_limit_and_skip_inactive_or_other_operation(tmp_path, listing):
    from index_scrapers.runner import review
    from index_scrapers.state import State
    visited = []
    def endpoint(request):
        visited.append(request.url.path)
        return httpx.Response(410)
    path = tmp_path / "state.sqlite"
    with State(path) as state:
        for identifier, operation, status in [("10000", "rent", "active"), ("10001", "rent", "active"), ("10002", "rent", "active"), ("10003", "rent", "removed"), ("10004", "sale", "active")]:
            candidate = deepcopy(listing)
            candidate.update(id=f"idealista:{identifier}", url=f"https://www.idealista.com/inmueble/{identifier}/", status=status)
            candidate["property"]["operation"] = operation
            state.observe(candidate)
            if identifier != "10000":
                state.check(candidate["id"], 404, "2026-10-12T13:00:00Z")
    with State(path) as state, httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        report = review(state, client, "rent", "2026-10-12T14:00:00Z", mode="withdrawals", limit=1)
        assert visited == ["/inmueble/10001/"]
        assert report["withdrawals"]["due_rechecks"] == 1
        assert report["withdrawals"]["sweep_remaining"] == 1
        assert report["withdrawals"]["limit"] == 1
    with State(path) as state, httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        review(state, client, "rent", "2026-10-12T15:00:00Z", mode="withdrawals", limit=1)
        assert visited[-1] == "/inmueble/10002/"
        review(state, client, "rent", "2026-10-12T16:00:00Z", mode="withdrawals", limit=1)
        assert visited[-1] == "/inmueble/10000/"


@pytest.mark.parametrize("status", [200, 403, 429, 503, None])
def test_withdrawal_recheck_recovery_or_temporary_error_does_not_queue_removal(tmp_path, listing, status):
    from index_scrapers.runner import review
    from index_scrapers.state import State
    path = tmp_path / "state.sqlite"
    with State(path) as state, httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404))) as client:
        state.observe(listing)
        state.acknowledge(state.pending(), [listing["id"]])
        review(state, client, "rent", "2026-10-12T13:00:00Z", mode="withdrawals")
    def endpoint(request):
        if status is None:
            raise httpx.ReadTimeout("offline fixture", request=request)
        return httpx.Response(status, text="<html></html>")
    with State(path) as state, httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        report = review(state, client, "rent", "2026-10-12T14:00:00Z", mode="withdrawals")
        assert report["changed"] == 0
        assert state.catalog()[0]["status"] == "active"
        assert state.pending() == []
        assert state.missing_checks() == []
        assert review(state, client, "rent", "2026-10-12T15:00:00Z", mode="withdrawals")["checked"] == 0


def test_withdrawal_cursor_does_not_skip_an_interrupted_request(tmp_path, listing):
    from index_scrapers.runner import review
    from index_scrapers.state import State
    path = tmp_path / "state.sqlite"
    visited = []
    def endpoint(request):
        visited.append(request.url.path)
        if len(visited) == 2:
            raise KeyboardInterrupt
        return httpx.Response(200, text="<html></html>")
    with State(path) as state, httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        for identifier in ["10001", "10002", "10003"]:
            candidate = deepcopy(listing)
            candidate.update(id=f"idealista:{identifier}", url=f"https://www.idealista.com/inmueble/{identifier}/")
            state.observe(candidate)
        with pytest.raises(KeyboardInterrupt):
            review(state, client, "rent", "2026-10-12T13:00:00Z", mode="withdrawals", limit=2)
    with State(path) as state, httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        report = review(state, client, "rent", "2026-10-12T14:00:00Z", mode="withdrawals", limit=2)
        assert visited == ["/inmueble/10001/", "/inmueble/10002/", "/inmueble/10002/", "/inmueble/10003/"]
        assert report["withdrawals"]["sweep_remaining"] == 0


def test_withdrawal_http_200_captcha_is_a_temporary_error_not_a_success(tmp_path, listing):
    from index_scrapers.runner import review
    from index_scrapers.state import State
    html = (Path(__file__).parent / "fixtures/blocked.synthetic.html").read_text(encoding="utf-8")
    path = tmp_path / "state.sqlite"
    with State(path) as state, httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404))) as client:
        state.observe(listing)
        state.acknowledge(state.pending(), [listing["id"]])
        review(state, client, "rent", "2026-10-12T13:00:00Z", mode="withdrawals")
    with State(path) as state, httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=html))) as client:
        report = review(state, client, "rent", "2026-10-12T14:00:00Z", mode="withdrawals")
        assert report["anomalies"] == [{"portal": listing["portal"], "reason": "idealista: HTTP challenge"}]
        assert report["changed"] == 0
        assert state.catalog()[0]["status"] == "active"
        assert state.pending() == []
        assert state.missing_checks() == []
        assert state.check(listing["id"], 410, "2026-10-12T15:00:00Z") is False
