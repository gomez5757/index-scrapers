import json
from pathlib import Path
import pytest


def test_cli_snapshot_does_not_create_empty_state_when_source_missing(tmp_path, capsys):
    from index_scrapers.cli import main
    assert main(["snapshot", "--state", str(tmp_path / "missing.sqlite"), "--snapshot-dir", str(tmp_path / "artifact")]) == 1
    assert not (tmp_path / "artifact/catalog.sqlite.gz").exists()


def test_cli_snapshot_exports_existing_pending_state(tmp_path, listing):
    from index_scrapers.cli import main
    from index_scrapers.state import State
    with State(tmp_path / "state.sqlite") as state:
        state.observe(listing)
    assert main(["snapshot", "--state", str(tmp_path / "state.sqlite"), "--snapshot-dir", str(tmp_path / "artifact")]) == 0
    assert (tmp_path / "artifact/catalog.sqlite.gz").exists()


def test_cli_invalid_target_records_failure_without_exposing_environment_token(tmp_path, monkeypatch, capsys):
    from index_scrapers.cli import main
    monkeypatch.setenv("INGEST_TOKEN", "secret-value-that-must-not-appear")
    path = tmp_path / "targets.json"
    path.write_text('[{"province":"99","municipality":"99999"}]')
    report = tmp_path / "health.json"
    result = main(["discover", "--operation", "rent", "--state", str(tmp_path / "state.sqlite"), "--targets", str(path), "--health", str(report)])
    assert result == 1
    assert "secret-value-that-must-not-appear" not in capsys.readouterr().out
    assert json.loads(report.read_text())["anomalies"]


def test_cli_delivers_previously_persisted_queue_without_loading_targets(tmp_path, listing, monkeypatch):
    from index_scrapers.cli import main
    from index_scrapers.state import State
    import httpx
    monkeypatch.setenv("INGEST_URL", "https://api.example.test/ingest")
    monkeypatch.setenv("INGEST_TOKEN", "example-test-token")
    path = tmp_path / "state.sqlite"
    with State(path) as state:
        state.observe(listing)
    original_client = httpx.Client
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"accepted": [{"id": listing["id"], "outcome": "written"}], "pending": [], "rows_written": 12, "budget_exhausted": False}))
    monkeypatch.setattr("index_scrapers.cli.httpx.Client", lambda **kwargs: original_client(transport=transport, **kwargs))
    assert main(["deliver", "--state", str(path), "--targets", "missing-targets.json", "--health", str(tmp_path / "health.json")]) == 0
    with State(path) as state:
        assert state.pending() == []


def test_cli_deliver_limit_five_preserves_sixth_listing(tmp_path, listing, monkeypatch, capsys):
    from copy import deepcopy
    from index_scrapers.cli import main
    from index_scrapers.state import State
    import httpx

    monkeypatch.setenv("INGEST_URL", "https://api.example.test/ingest")
    monkeypatch.setenv("INGEST_TOKEN", "example-test-token")
    path = tmp_path / "state.sqlite"
    with State(path) as state:
        for identifier in range(6):
            item = deepcopy(listing)
            item["id"] = f"idealista:{identifier}"
            state.observe(item)
    batches = []
    def endpoint(request):
        ids = [item["id"] for item in json.loads(request.content)["listings"]]
        batches.append(ids)
        return httpx.Response(200, json={"accepted": [{"id": identifier, "outcome": "written"} for identifier in ids],
            "pending": [], "rows_written": 12, "budget_exhausted": False})
    original_client = httpx.Client
    transport = httpx.MockTransport(endpoint)
    monkeypatch.setattr("index_scrapers.cli.httpx.Client", lambda **kwargs: original_client(transport=transport, **kwargs))
    report = tmp_path / "health.json"
    assert main(["deliver", "--limit", "5", "--state", str(path), "--health", str(report)]) == 0
    assert batches == [["idealista:0", "idealista:1", "idealista:2", "idealista:3", "idealista:4"]]
    with State(path) as state:
        assert [item["listing"]["id"] for item in state.pending()] == ["idealista:5"]
    output = capsys.readouterr().out
    observed = json.loads(output)
    assert observed == json.loads(report.read_text())
    assert observed["delivery"]["accepted"] == 5
    assert observed["delivery"]["pending"] == 1
    assert "example-test-token" not in output
    assert "Authorization" not in output


def test_cli_deliver_defaults_to_one_hundred_items(tmp_path, listing, monkeypatch):
    from copy import deepcopy
    from index_scrapers.cli import main
    from index_scrapers.state import State
    import httpx
    monkeypatch.setenv("INGEST_URL", "https://api.example.test/ingest")
    monkeypatch.setenv("INGEST_TOKEN", "example-test-token")
    path = tmp_path / "state.sqlite"
    with State(path) as state:
        for identifier in range(101):
            item = deepcopy(listing)
            item["id"] = f"idealista:{identifier}"
            state.observe(item)
    delivered = []
    def endpoint(request):
        ids = [item["id"] for item in json.loads(request.content)["listings"]]
        assert len(ids) <= 5
        delivered.extend(ids)
        return httpx.Response(200, json={"accepted": [{"id": identifier, "outcome": "written"} for identifier in ids],
            "pending": [], "rows_written": 12, "budget_exhausted": False})
    original_client = httpx.Client
    transport = httpx.MockTransport(endpoint)
    monkeypatch.setattr("index_scrapers.cli.httpx.Client", lambda **kwargs: original_client(transport=transport, **kwargs))
    assert main(["deliver", "--state", str(path), "--health", str(tmp_path / "health.json")]) == 0
    assert len(delivered) == 100
    with State(path) as state:
        assert [item["listing"]["id"] for item in state.pending()] == ["idealista:100"]


def test_cli_implicit_delivery_keeps_batch_limit_separate_from_review_limit(tmp_path, listing, monkeypatch):
    from copy import deepcopy
    from index_scrapers.cli import main
    from index_scrapers.state import State
    import httpx
    monkeypatch.setenv("INGEST_URL", "https://api.example.test/ingest")
    monkeypatch.setenv("INGEST_TOKEN", "example-test-token")
    monkeypatch.setattr("index_scrapers.cli.load_targets", lambda *args: [])
    monkeypatch.setattr("index_scrapers.cli.review", lambda *args, **kwargs: {})
    path = tmp_path / "state.sqlite"
    with State(path) as state:
        for identifier in range(101):
            item = deepcopy(listing)
            item["id"] = f"idealista:{identifier}"
            state.observe(item)
    delivered = []
    def endpoint(request):
        ids = [item["id"] for item in json.loads(request.content)["listings"]]
        delivered.extend(ids)
        return httpx.Response(200, json={"accepted": [{"id": identifier, "outcome": "written"} for identifier in ids],
            "pending": [], "rows_written": 12, "budget_exhausted": False})
    original_client = httpx.Client
    monkeypatch.setattr("index_scrapers.cli.httpx.Client", lambda **kwargs: original_client(transport=httpx.MockTransport(endpoint), **kwargs))
    assert main(["prices", "--limit", "1", "--state", str(path), "--health", str(tmp_path / "health.json")]) == 0
    assert len(delivered) == 101
    with State(path) as state:
        assert state.pending_count() == 0


def test_cli_delivery_failure_reports_type_without_request_credentials(tmp_path, listing, monkeypatch, capsys):
    from index_scrapers.cli import main
    from index_scrapers.state import State
    import httpx
    secret = "secret-delivery-token-that-must-not-appear"
    monkeypatch.setenv("INGEST_URL", "https://api.example.test/ingest")
    monkeypatch.setenv("INGEST_TOKEN", secret)
    path = tmp_path / "state.sqlite"
    with State(path) as state:
        state.observe(listing)
    def endpoint(request):
        raise httpx.RequestError(f"Authorization: Bearer {secret}", request=request)
    original_client = httpx.Client
    monkeypatch.setattr("index_scrapers.cli.httpx.Client", lambda **kwargs: original_client(transport=httpx.MockTransport(endpoint), **kwargs))
    report = tmp_path / "health.json"
    assert main(["deliver", "--limit", "5", "--state", str(path), "--health", str(report)]) == 1
    output = capsys.readouterr().out
    assert json.loads(output) == json.loads(report.read_text())
    assert json.loads(output)["anomalies"] == [{"portal": "runner", "reason": "RequestError"}]
    assert secret not in output
    assert "Authorization" not in output
    assert secret not in report.read_text()
    with State(path) as state:
        assert state.pending_count() == 1


@pytest.mark.parametrize("no_delivery", [True, False])
def test_health_checks_new_real_listings_without_changing_catalog_queue_or_progress(tmp_path, listing, monkeypatch, no_delivery):
    from index_scrapers.cli import main
    from index_scrapers.state import State
    import httpx

    fixtures = Path(__file__).parent / "fixtures/captures"
    pages = {
        "www.idealista.com": (fixtures / "idealista.browser-20261012.html").read_text(encoding="utf-8"),
        "www.fotocasa.es": (fixtures / "fotocasa.http-200.html").read_text(encoding="utf-8"),
    }
    targets = [
        {"portal": "idealista", "operation": "rent", "province": "28", "municipality": "28079", "city_slug": "madrid", "url": "https://www.idealista.com/alquiler-viviendas/madrid-madrid/"},
        {"portal": "fotocasa", "operation": "rent", "province": "28", "municipality": "28079", "city_slug": "madrid", "url": "https://www.fotocasa.es/es/alquiler/viviendas/madrid-capital/todas-las-zonas/l"},
    ]
    targets_path = tmp_path / "targets.json"
    targets_path.write_text(json.dumps(targets), encoding="utf-8")
    path = tmp_path / "state.sqlite"
    progress_key = "initial:idealista:28079:rent"
    listing["photo_hash"] = "0123456789abcdef"
    with State(path) as state:
        state.observe(listing)
        state.progress(progress_key, {"url": "https://www.idealista.com/alquiler-viviendas/madrid-madrid/pagina-7.htm", "done": False})
        catalog_before = state.catalog()
        queue_before = state.pending()
        progress_before = list(state.db.execute("SELECT key,value FROM progress ORDER BY key"))

    def endpoint(request):
        assert request.method == "GET"
        return httpx.Response(200, text=pages[request.url.host])

    original_client = httpx.Client
    transport = httpx.MockTransport(endpoint)
    monkeypatch.setattr("index_scrapers.cli.httpx.Client", lambda **kwargs: original_client(transport=transport, **kwargs))
    monkeypatch.setenv("INGEST_URL", "https://api.example.test/ingest")
    monkeypatch.setenv("INGEST_TOKEN", "example-test-token")
    report_path = tmp_path / "health.json"
    args = ["health", "--state", str(path), "--targets", str(targets_path),
        "--municipalities", str(Path(__file__).parents[1] / "data/municipalities.csv"), "--health", str(report_path)]
    if no_delivery:
        args.append("--no-delivery")
    result = main(args)

    with State(path) as state:
        assert state.catalog() == catalog_before
        assert state.pending() == queue_before
        assert state.pending_count() == 1
        assert list(state.db.execute("SELECT key,value FROM progress ORDER BY key")) == progress_before
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result == 0
    assert report["results"] > 1
    assert report["changed"] == 0
