from copy import deepcopy
import httpx
import pytest


def response(accepted, pending, budget=False):
    return {"accepted": [{"id": identifier, "outcome": "written"} for identifier in accepted], "pending": pending, "rows_written": 12, "budget_exhausted": budget}


def test_partial_delivery_only_acknowledges_confirmed_ids(tmp_path, listing):
    from index_scrapers.delivery import flush
    from index_scrapers.state import State
    second = deepcopy(listing)
    second["id"] = "fotocasa:2"
    def endpoint(request):
        import json
        body = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer example-test-token"
        assert [item["id"] for item in body["listings"]] == [listing["id"], "fotocasa:2"]
        return httpx.Response(200, json=response([listing["id"]], ["fotocasa:2"], True))
    with State(tmp_path / "state.sqlite") as state, httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        state.observe(listing)
        state.observe(second)
        assert flush(state, client, "https://api.example.test/ingest", "example-test-token")["accepted"] == 1
        assert [item["listing"]["id"] for item in state.pending()] == ["fotocasa:2"]


@pytest.mark.parametrize("body", [
    response(["foreign:1"], []), response([], []),
    response(["idealista:10001"], ["idealista:10001"]),
    {"accepted": [{"id": "idealista:10001", "outcome": "unknown"}], "pending": [], "rows_written": 1, "budget_exhausted": False},
])
def test_invalid_ack_leaves_entire_batch_pending(tmp_path, listing, body):
    from index_scrapers.delivery import flush, InvalidAcknowledgement
    from index_scrapers.state import State
    with State(tmp_path / "state.sqlite") as state, httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as client:
        state.observe(listing)
        with pytest.raises(InvalidAcknowledgement):
            flush(state, client, "https://api.example.test/ingest", "example-test-token")
        assert len(state.pending()) == 1


def test_network_failure_and_http_failure_keep_pending(tmp_path, listing):
    from index_scrapers.delivery import flush
    from index_scrapers.state import State
    with State(tmp_path / "state.sqlite") as state, httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as client:
        state.observe(listing)
        with pytest.raises(httpx.HTTPStatusError):
            flush(state, client, "https://api.example.test/ingest", "example-test-token")
        assert len(state.pending()) == 1


def test_all_revisions_are_sent_in_chronological_order(tmp_path, listing):
    from index_scrapers.delivery import flush
    from index_scrapers.state import State
    prices = []
    def endpoint(request):
        import json
        body = json.loads(request.content)
        prices.extend(item["price"] for item in body["listings"])
        return httpx.Response(200, json=response([item["id"] for item in body["listings"]], []))
    with State(tmp_path / "state.sqlite") as state, httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        state.observe(listing)
        newer = deepcopy(listing)
        newer["price"] = 1300
        state.observe(newer)
        flush(state, client, "https://api.example.test/ingest", "example-test-token")
        assert prices == [1250, 1300]
        assert state.pending() == []


def test_pending_count_includes_queue_beyond_first_batch(tmp_path, listing):
    from index_scrapers.delivery import flush
    from index_scrapers.state import State
    def endpoint(request):
        import json
        ids = [item["id"] for item in json.loads(request.content)["listings"]]
        return httpx.Response(200, json=response([], ids, True))
    with State(tmp_path / "state.sqlite") as state, httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        for identifier in range(7):
            item = deepcopy(listing)
            item["id"] = f"idealista:{identifier}"
            state.observe(item)
        result = flush(state, client, "https://api.example.test/ingest", "example-test-token")
        assert result["pending"] == 7


@pytest.mark.parametrize("limit,batch_sizes", [(1, [1]), (2, [2]), (3, [3]), (4, [4]), (5, [5]), (7, [5, 2])])
def test_max_items_caps_total_and_final_batch_without_dropping_remaining(tmp_path, listing, limit, batch_sizes):
    from index_scrapers.delivery import flush
    from index_scrapers.state import State
    import json
    batches = []
    def endpoint(request):
        ids = [item["id"] for item in json.loads(request.content)["listings"]]
        batches.append(ids)
        return httpx.Response(200, json=response(ids, []))
    with State(tmp_path / "state.sqlite") as state, httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        for identifier in range(8):
            item = deepcopy(listing)
            item["id"] = f"idealista:{identifier}"
            state.observe(item)
        result = flush(state, client, "https://api.example.test/ingest", "example-test-token", max_items=limit)
        assert [len(batch) for batch in batches] == batch_sizes
        assert [identifier for batch in batches for identifier in batch] == [f"idealista:{identifier}" for identifier in range(limit)]
        assert result["accepted"] == limit
        assert result["pending"] == 8 - limit
        assert state.pending_count() == 8 - limit
        assert state.pending()[0]["listing"]["id"] == f"idealista:{limit}"
    with State(tmp_path / "state.sqlite") as state:
        assert state.pending_count() == 8 - limit
        assert state.pending()[0]["listing"]["id"] == f"idealista:{limit}"


@pytest.mark.parametrize("limit", [0, -1, True, False, 1.5])
def test_invalid_max_items_cannot_deliver_or_ack_pending(tmp_path, listing, limit):
    from index_scrapers.delivery import flush
    from index_scrapers.state import State
    def endpoint(request):
        raise AssertionError("invalid item limit must not send HTTP")
    with State(tmp_path / "state.sqlite") as state, httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        state.observe(listing)
        before = state.pending()
        with pytest.raises(ValueError):
            flush(state, client, "https://api.example.test/ingest", "example-test-token", max_items=limit)
        assert state.pending() == before


def test_max_items_preserves_existing_max_batches_guard(tmp_path, listing):
    from index_scrapers.delivery import flush
    from index_scrapers.state import State
    import json
    def endpoint(request):
        ids = [item["id"] for item in json.loads(request.content)["listings"]]
        return httpx.Response(200, json=response(ids, []))
    with State(tmp_path / "state.sqlite") as state, httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        for identifier in range(8):
            item = deepcopy(listing)
            item["id"] = f"idealista:{identifier}"
            state.observe(item)
        result = flush(state, client, "https://api.example.test/ingest", "example-test-token", max_batches=1, max_items=7)
        assert result["accepted"] == 5
        assert state.pending_count() == 3


def test_unbounded_item_delivery_still_stops_after_one_hundred_batches(tmp_path, listing):
    from index_scrapers.delivery import flush
    from index_scrapers.state import State
    import json
    batches = []
    def endpoint(request):
        ids = [item["id"] for item in json.loads(request.content)["listings"]]
        batches.append(ids)
        return httpx.Response(200, json=response(ids, []))
    with State(tmp_path / "state.sqlite") as state, httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        for identifier in range(501):
            item = deepcopy(listing)
            item["id"] = f"idealista:{identifier}"
            state.observe(item)
        result = flush(state, client, "https://api.example.test/ingest", "example-test-token")
        assert len(batches) == 100
        assert all(len(batch) == 5 for batch in batches)
        assert result["accepted"] == 500
        assert result["pending"] == 1
    with State(tmp_path / "state.sqlite") as state:
        assert [item["listing"]["id"] for item in state.pending()] == ["idealista:500"]
