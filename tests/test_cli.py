import json
from pathlib import Path


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
