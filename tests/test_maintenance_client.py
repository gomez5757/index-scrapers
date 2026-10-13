import json

import httpx
import pytest

from scripts import maintain_fts


TOKEN = "maintenance-secret-that-must-not-appear"


def maintenance_result(operation_id=None, **changes):
    result = {
        "needed": False, "performed": False, "replayed": False, "receipt": None,
        "reservation": 0, "reserved": 10, "actual": None, "rows_written": 0,
        "segments": 127, "pending_merge": False, "budget_exhausted": False,
        "capacity_exhausted": False, "retry_needed": False, "halted": False,
        "schema_drift": False, "config_drift": False, "input_invalid": False,
        "cost_model_exceeded": False, "model_supported": True,
        "model_version": "fts5-whole-index-v1", "conditional": True,
        "sqlite_version": None, "engine_verified": False,
    }
    result.update(changes)
    if result["performed"]:
        result["receipt"] = {
            "opid": operation_id, "claim": TOKEN, "reservation": result["reservation"],
            "actual": result["actual"], "before_hex": "00", "after_hex": "00", "config": TOKEN,
        }
    return result


@pytest.fixture
def invoke(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("INGEST_URL", "https://api.example.test/private/ingest")
    monkeypatch.setenv("INGEST_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_RUN_ID", "17")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    original_client = httpx.Client
    health = tmp_path / "maintenance-health.json"

    def run(handler, *args):
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(maintain_fts.httpx, "Client", lambda **kwargs: original_client(transport=transport, **kwargs))
        code = maintain_fts.main(["--health", str(health), *args])
        output = capsys.readouterr().out
        report = json.loads(output)
        assert report == json.loads(health.read_text(encoding="utf-8"))
        assert TOKEN not in output
        assert "Authorization" not in output
        assert "before_hex" not in output and "after_hex" not in output
        assert not health.with_suffix(".tmp").exists()
        return code, report

    return run


def test_unneeded_maintenance_stops_after_one_authenticated_request(invoke):
    requests = []

    def endpoint(request):
        requests.append(request)
        assert request.method == "POST"
        assert str(request.url) == "https://api.example.test/private/maintenance"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert json.loads(request.content) == {"operation_id": 170020}
        return httpx.Response(200, json=maintenance_result())

    code, report = invoke(endpoint)
    assert code == 0 and len(requests) == 1
    assert report["rows_written"] == report["reservations"] == 0
    assert report["maintenance"]["conditional"] is True
    assert report["maintenance"]["engine_verified"] is False
    assert report["maintenance"]["sqlite_version"] is None


@pytest.mark.parametrize("segments,pending_merge", [(128, False), (2, True)])
def test_remaining_work_uses_next_stable_operation_and_counts_only_current_writes(invoke, segments, pending_merge):
    operation_ids = []

    def endpoint(request):
        operation_id = json.loads(request.content)["operation_id"]
        operation_ids.append(operation_id)
        first = len(operation_ids) == 1
        return httpx.Response(200, json=maintenance_result(operation_id, needed=True, performed=True,
            reservation=80, actual=8, rows_written=8, segments=segments if first else 127,
            pending_merge=pending_merge if first else False))

    code, report = invoke(endpoint)
    assert code == 0
    assert operation_ids == [170020, 170021]
    assert report["rows_written"] == 16
    assert report["reservations"] == 160
    assert report["performed"] == 2


def test_lost_response_reuses_operation_id_and_never_recounts_receipt_cost(invoke):
    operation_ids = []

    def endpoint(request):
        operation_id = json.loads(request.content)["operation_id"]
        operation_ids.append(operation_id)
        if len(operation_ids) == 1:
            raise httpx.RequestError(f"Authorization: Bearer {TOKEN}", request=request)
        return httpx.Response(200, json=maintenance_result(operation_id, needed=True, performed=True,
            replayed=True, reservation=80, actual=8, rows_written=0))

    code, report = invoke(endpoint)
    assert code == 1
    assert report["anomalies"] == [{"reason": "RequestError"}]
    code, report = invoke(endpoint)
    assert code == 0
    assert operation_ids == [170020, 170020]
    assert report["rows_written"] == report["reservations"] == report["performed"] == 0
    assert report["replayed"] == 1
    assert report["maintenance"]["receipt_actual"] == 8


def test_four_steps_with_remaining_work_block_delivery(invoke):
    operation_ids = []

    def endpoint(request):
        operation_id = json.loads(request.content)["operation_id"]
        operation_ids.append(operation_id)
        return httpx.Response(200, json=maintenance_result(operation_id, needed=True, performed=True,
            reservation=80, actual=8, rows_written=8, segments=128))

    code, report = invoke(endpoint)
    assert code == 1
    assert operation_ids == [170020, 170021, 170022, 170023]
    assert report["rows_written"] == 32 and report["reservations"] == 320
    assert report["anomalies"] == [{"reason": "MaintenanceStepLimit"}]


@pytest.mark.parametrize("flag", ["budget_exhausted", "capacity_exhausted", "retry_needed", "halted",
    "schema_drift", "config_drift", "input_invalid", "cost_model_exceeded"])
def test_server_stop_flags_block_without_another_operation(invoke, flag):
    requests = []

    def endpoint(request):
        requests.append(request)
        return httpx.Response(200, json=maintenance_result(**{flag: True}))

    code, report = invoke(endpoint)
    assert code == 1 and len(requests) == 1
    assert report["maintenance"][flag] is True
    assert report["anomalies"] == [{"reason": "MaintenanceBlocked"}]


@pytest.mark.parametrize("changes", [{"reservation": 85001}, {"model_supported": False}])
def test_capacity_and_unsupported_model_block_even_without_server_stop_flag(invoke, changes):
    code, report = invoke(lambda _: httpx.Response(200, json=maintenance_result(**changes)))
    assert code == 1
    assert report["anomalies"] == [{"reason": "MaintenanceBlocked"}]


@pytest.mark.parametrize("value", [True, -1, 1.0, None])
def test_invalid_physical_counter_blocks(invoke, value):
    code, report = invoke(lambda _: httpx.Response(200, json=maintenance_result(rows_written=value)))
    assert code == 1
    assert report["anomalies"] == [{"reason": "InvalidMaintenanceResult"}]


def test_missing_response_field_blocks(invoke):
    result = maintenance_result()
    del result["pending_merge"]
    code, report = invoke(lambda _: httpx.Response(200, json=result))
    assert code == 1
    assert report["anomalies"] == [{"reason": "InvalidMaintenanceResult"}]


def test_http_failure_and_untrusted_error_body_never_leak(invoke):
    code, report = invoke(lambda _: httpx.Response(503, json={"error": TOKEN}))
    assert code == 1
    assert report["anomalies"] == [{"reason": "HTTPStatusError"}]


@pytest.mark.parametrize("key,value", [
    ("INGEST_URL", "http://api.example.test/ingest"),
    ("INGEST_URL", f"https://user:{TOKEN}@api.example.test/ingest"),
    ("INGEST_URL", "https://api.example.test/ingest?token=private"),
    ("INGEST_URL", "https://api.example.test/other"),
    ("GITHUB_RUN_ID", "0"), ("GITHUB_RUN_ID", "900719925475"),
    ("GITHUB_RUN_ATTEMPT", "0"), ("GITHUB_RUN_ATTEMPT", "1000"),
])
def test_invalid_environment_rejected_before_http(invoke, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    code, report = invoke(lambda _: pytest.fail("invalid environment must not make requests"))
    assert code == 1
    assert report["anomalies"] == [{"reason": "ValueError"}]


@pytest.mark.parametrize("limit", ["0", "5"])
def test_step_limit_is_bounded_before_http(invoke, limit):
    code, report = invoke(lambda _: pytest.fail("invalid step limit must not make requests"), "--max-steps", limit)
    assert code == 1
    assert report["anomalies"] == [{"reason": "ValueError"}]
