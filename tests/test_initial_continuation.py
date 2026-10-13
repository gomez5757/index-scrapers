from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest

from index_scrapers.runner import discover
from index_scrapers.state import State


NOW = "2026-10-13T10:00:00Z"
BASE = {"portal": "idealista", "operation": "rent", "province": "28", "municipality": "28079", "city_slug": "madrid", "priority": "major"}
HTML = (Path(__file__).parent / "fixtures/idealista.synthetic.html").read_text(encoding="utf-8")


def target(name, **values):
    return {**BASE, "url": f"https://www.idealista.com/{name}/1", **values}


def cursor_key(item):
    return f"initial:{item['portal']}:{item['municipality']}:{item['operation']}"


def page(identifier, next_url=None):
    return HTML.replace("10001", str(identifier)) + (f'<a rel="next" href="{next_url}">Siguiente</a>' if next_url else "")


def test_initial_budget_is_global_and_phase_survives_restart(tmp_path):
    targets = [target("a"), target("b", municipality="08019", province="08"), target("capital", municipality="01059", province="01", priority="capital"), target("sale", operation="sale")]
    visited = []

    def endpoint(request):
        visited.append(request.url.path)
        name, number = request.url.path.strip("/").split("/")
        identifier = {"a": 10011, "b": 10021, "capital": 10031, "sale": 10041}[name] + int(number)
        html = page(identifier, f"/{name}/2" if name in {"a", "b"} and number == "1" else None)
        return httpx.Response(200, text=html.replace("/mes", "") if name == "sale" else html)

    path = tmp_path / "state.sqlite"
    with httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        for _ in range(2):
            with State(path) as state:
                report = discover(state, client, targets, NOW, max_pages=2, hash_images=False, resume=True)
                assert report["initial"]["pages"] == 2
                assert report["initial"]["status"] == "pending"
                assert report["initial"]["phase"] == "rent:major"
        assert set(visited[:2]) == {"/a/1", "/b/1"}
        assert set(visited[2:]) == {"/a/2", "/b/2"}
        with State(path) as state:
            report = discover(state, client, targets, NOW, max_pages=2, hash_images=False, resume=True)
            assert visited[-2:] == ["/capital/1", "/sale/1"]
            assert report["initial"]["status"] == "completed"
            assert all(item["status"] == "completed" for item in report["initial"]["targets"])


def test_blocked_target_is_durable_without_starving_remaining_phases(tmp_path):
    targets = [target("blocked", municipality="08019", province="08"), target("available"), target("sale", operation="sale")]
    visited = []
    recovered = False

    def endpoint(request):
        visited.append(request.url.path)
        name = request.url.path.strip("/").split("/")[0]
        html = page({"blocked": 10012, "available": 10011, "sale": 10013}[name])
        return httpx.Response(403) if name == "blocked" and not recovered else httpx.Response(200, text=html.replace("/mes", "") if name == "sale" else html)

    path = tmp_path / "state.sqlite"
    with httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        for _ in range(3):
            with State(path) as state:
                report = discover(state, client, targets, NOW, max_pages=1, hash_images=False, resume=True)
                assert report["initial"]["status"] != "completed"
        assert visited == ["/blocked/1", "/available/1", "/sale/1"]
        assert report["initial"]["status"] == "blocked"
        blocked = next(item for item in report["initial"]["targets"] if item["status"] == "blocked")
        assert blocked["reason"] == "http_403"
        assert blocked["retry_after"] == "2026-10-13T11:00:00Z"
        with State(path) as state:
            report = discover(state, client, targets, NOW, max_pages=1, hash_images=False, resume=True)
            assert report["initial"]["pages"] == 0
        recovered = True
        with State(path) as state:
            report = discover(state, client, targets, "2026-10-13T11:00:00Z", max_pages=1, hash_images=False, resume=True)
            assert report["initial"]["status"] == "completed"


def test_legacy_cursor_resumes_page_four_without_restarting(tmp_path):
    item = target("legacy")
    visited = []
    with State(tmp_path / "state.sqlite") as state:
        state.progress(cursor_key(item), {"url": "https://www.idealista.com/legacy/4", "done": False})
        with httpx.Client(transport=httpx.MockTransport(lambda request: visited.append(request.url.path) or httpx.Response(200, text=page(10011)))) as client:
            report = discover(state, client, [item], NOW, max_pages=1, hash_images=False, resume=True)
        assert visited == ["/legacy/4"]
        assert report["initial"]["status"] == "completed"
        assert state.progress(cursor_key(item))["done"] is True


@pytest.mark.parametrize("portal", ["fotocasa", "habitaclia"])
def test_declared_more_results_without_next_link_stays_blocked(tmp_path, monkeypatch, listing, portal):
    from index_scrapers import runner
    monkeypatch.setattr(runner, "parse", lambda *args: [deepcopy(listing)])
    domain = "www.fotocasa.es" if portal == "fotocasa" else "www.habitaclia.com"
    item = {**BASE, "portal": portal, "url": f"https://{domain}/search/1"}
    if portal == "fotocasa":
        data = {"initialSearch": {"params": {"pageNumber": 1}, "result": {"count": 100, "resultsV2": {"page": {"number": 1, "size": 30}, "totalItems": 100}}}}
        html = f'<script id="__initial_props__">{json.dumps(data)}</script>'
    else:
        data = {"initialSearchResultsPage": {"initialSearchContext": {"results": {"pagination": {"page": 1, "pageSize": 29, "totalCount": 100, "totalPages": 4}}}}}
        html = f'<script>window.__INITIAL_PROPS__ = JSON.parse({json.dumps(json.dumps(data))});</script>'
    with State(tmp_path / "state.sqlite") as state, httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=html))) as client:
        report = discover(state, client, [item], NOW, max_pages=1, hash_images=False, resume=True)
        assert report["initial"]["status"] == "blocked"
        cursor = state.progress(cursor_key(item))
        assert cursor["status"] == "blocked"
        assert cursor["done"] is False
        assert cursor["reason"] == "pagination_missing"
        assert cursor["pagination"]["total_results"] == 100
        assert state.pending_count() == 1


def test_pagination_cycle_is_detected_across_restarts(tmp_path):
    item = target("cycle")
    path = tmp_path / "state.sqlite"

    def endpoint(request):
        number = request.url.path.rsplit("/", 1)[1]
        return httpx.Response(200, text=page(10010 + int(number), f"/cycle/{2 if number == '1' else 1}"))

    with httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        with State(path) as state:
            discover(state, client, [item], NOW, max_pages=1, hash_images=False, resume=True)
        with State(path) as state:
            report = discover(state, client, [item], NOW, max_pages=1, hash_images=False, resume=True)
            assert report["initial"]["status"] == "blocked"
            assert state.progress(cursor_key(item))["reason"] == "pagination_cycle"


def test_time_budget_leaves_current_page_pending_for_idempotent_retry(tmp_path, monkeypatch):
    from index_scrapers import runner
    item = target("timeout")
    clock = [0]
    monkeypatch.setattr(runner, "monotonic", lambda: clock[0])

    def endpoint(request):
        clock[0] = 2
        return httpx.Response(200, text=page(10011))

    path = tmp_path / "state.sqlite"
    with State(path) as state, httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        report = discover(state, client, [item], NOW, max_pages=3, hash_images=False, resume=True, max_seconds=1)
        assert report["initial"]["status"] == "pending"
        assert report["initial"]["stop_reason"] == "time_limit"
        assert state.progress(cursor_key(item))["url"] == item["url"]
        assert state.progress(cursor_key(item))["done"] is False
    clock[0] = 0
    with State(path) as state, httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=page(10011)))) as client:
        report = discover(state, client, [item], NOW, max_pages=1, hash_images=False, resume=True, max_seconds=1)
        assert report["initial"]["status"] == "completed"
        assert state.pending_count() == 1


def test_initial_status_is_read_only_and_counts_due_blocked_targets(tmp_path, listing):
    from index_scrapers.runner import initial_status
    blocked = target("blocked")
    sale = target("sale", operation="sale")
    with State(tmp_path / "state.sqlite") as state:
        state.observe(listing)
        state.progress(cursor_key(blocked), {"url": blocked["url"], "done": False, "status": "blocked", "reason": "http_403", "retry_after": "2026-10-13T11:00:00Z"})
        changes = state.db.total_changes
        summary = initial_status(state, [blocked, sale], NOW)
        assert (summary["status"], summary["pending"], summary["blocked"], summary["eligible"], summary["phase"]) == ("pending", 1, 1, 1, "sale:major")
        due = initial_status(state, [blocked, sale], "2026-10-13T11:00:00Z")
        assert due["eligible"] == 2
        assert due["phase"] == "rent:major"
        assert state.db.total_changes == changes
        assert state.pending_count() == 1


def test_slow_image_stream_cannot_finish_initial_after_deadline(tmp_path, monkeypatch):
    from index_scrapers import runner
    clock = [0]
    monkeypatch.setattr(runner, "monotonic", lambda: clock[0])

    class SlowImage(httpx.SyncByteStream):
        def __iter__(self):
            yield b"first"
            clock[0] = 2
            yield b"last"

    def endpoint(request):
        if request.url.host == "img.idealista.com":
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, stream=SlowImage())
        return httpx.Response(200, text=page(10011))

    item = target("image")
    with State(tmp_path / "state.sqlite") as state, httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        report = discover(state, client, [item], NOW, max_pages=1, resume=True, max_seconds=1)
        assert report["initial"]["status"] == "pending"
        assert report["initial"]["stop_reason"] == "time_limit"
        assert state.pending_count() == 0
        assert state.progress(cursor_key(item))["url"] == item["url"]
