"""Bounded private FTS maintenance before ordinary delivery."""

import argparse
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit

import httpx


MAX_SAFE_INTEGER = 2**53 - 1
DAILY_LIMIT = 85000
SEGMENT_THRESHOLD = 128
MODEL_VERSION = "fts5-whole-index-v1"
STOP_FLAGS = (
    "budget_exhausted", "capacity_exhausted", "retry_needed", "halted",
    "schema_drift", "config_drift", "input_invalid", "cost_model_exceeded",
)
BOOL_FIELDS = ("needed", "performed", "replayed", "model_supported", *STOP_FLAGS)


class InvalidMaintenanceResult(ValueError):
    pass


class MaintenanceBlocked(RuntimeError):
    pass


class MaintenanceStepLimit(RuntimeError):
    pass


def maintenance_url(ingest_url):
    if not isinstance(ingest_url, str) or not ingest_url.endswith("/ingest") or any(character.isspace() for character in ingest_url):
        raise ValueError("invalid ingest URL")
    parsed = urlsplit(ingest_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("invalid ingest URL")
    parsed.port
    endpoint = urlunsplit((parsed.scheme, parsed.netloc, parsed.path[:-len("/ingest")] + "/maintenance", "", ""))
    if not httpx.URL(endpoint).host:
        raise ValueError("invalid maintenance URL")
    return endpoint


def operation_ids(environ, max_steps):
    if type(max_steps) is not int or not 1 <= max_steps <= 4:
        raise ValueError("step limit must be between one and four")
    values = []
    for name in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        raw = environ.get(name, "")
        if not isinstance(raw, str) or not re.fullmatch(r"[0-9]+", raw):
            raise ValueError("invalid workflow identity")
        values.append(int(raw))
    run_id, attempt = values
    if not 1 <= run_id <= MAX_SAFE_INTEGER or not 1 <= attempt <= 999:
        raise ValueError("invalid workflow identity")
    ids = [run_id * 10000 + attempt * 10 + step for step in range(max_steps)]
    if any(operation_id > MAX_SAFE_INTEGER for operation_id in ids):
        raise ValueError("unsafe operation identity")
    return ids


def _integer(value):
    return type(value) is int and 0 <= value <= MAX_SAFE_INTEGER


def validate_result(result, operation_id):
    required = {*BOOL_FIELDS, "receipt", "reservation", "reserved", "actual", "rows_written",
        "segments", "pending_merge", "model_version", "conditional", "sqlite_version", "engine_verified"}
    if not isinstance(result, dict) or not required <= result.keys():
        raise InvalidMaintenanceResult()
    if any(type(result[name]) is not bool for name in BOOL_FIELDS):
        raise InvalidMaintenanceResult()
    if result["model_version"] != MODEL_VERSION or result["conditional"] is not True or result["engine_verified"] is not False or result["sqlite_version"] is not None:
        raise InvalidMaintenanceResult()
    if any(not _integer(result[name]) for name in ("reservation", "reserved", "rows_written")):
        raise InvalidMaintenanceResult()
    if result["actual"] is not None and not _integer(result["actual"]):
        raise InvalidMaintenanceResult()
    if result["segments"] is not None and not _integer(result["segments"]):
        raise InvalidMaintenanceResult()
    if result["pending_merge"] is not None and type(result["pending_merge"]) is not bool:
        raise InvalidMaintenanceResult()
    receipt = result["receipt"]
    if result["performed"]:
        if not result["needed"] or not isinstance(receipt, dict) or not _integer(result["actual"]) or result["reservation"] < 1:
            raise InvalidMaintenanceResult()
        if not {"opid", "claim", "reservation", "before_hex", "after_hex", "config", "actual"} <= receipt.keys():
            raise InvalidMaintenanceResult()
        if type(receipt["opid"]) is not int or receipt["opid"] != operation_id or type(receipt["reservation"]) is not int or receipt["reservation"] != result["reservation"] or type(receipt["actual"]) is not int or receipt["actual"] != result["actual"]:
            raise InvalidMaintenanceResult()
        if any(not isinstance(receipt[name], str) or not receipt[name] for name in ("claim", "before_hex", "after_hex", "config")):
            raise InvalidMaintenanceResult()
        if result["segments"] is None or result["pending_merge"] is None:
            raise InvalidMaintenanceResult()
        if not result["replayed"] and result["actual"] != result["rows_written"]:
            raise InvalidMaintenanceResult()
    elif result["replayed"] or receipt is not None or result["actual"] is not None:
        raise InvalidMaintenanceResult()
    if not result["needed"] and (result["pending_merge"] is True or result["segments"] is not None and result["segments"] >= SEGMENT_THRESHOLD):
        raise InvalidMaintenanceResult()
    return result


def run_maintenance(client, endpoint, token, ids, report):
    for operation_id in ids:
        report["operation_ids"].append(operation_id)
        response = client.post(endpoint, json={"operation_id": operation_id}, headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        result = validate_result(response.json(), operation_id)
        report["steps"] += 1
        report["rows_written"] += result["rows_written"]
        report["performed"] += int(result["performed"] and not result["replayed"])
        report["replayed"] += int(result["replayed"])
        if result["performed"] and not result["replayed"]:
            report["reservations"] += result["reservation"]
        report["maintenance"] = {name: result[name] for name in (
            *BOOL_FIELDS, "reservation", "reserved", "segments", "pending_merge",
            "model_version", "conditional", "sqlite_version", "engine_verified",
        )}
        report["maintenance"]["receipt_actual"] = result["actual"]
        report["counters_complete"] = True
        if any(result[name] for name in STOP_FLAGS) or not result["model_supported"] or result["reservation"] > DAILY_LIMIT or result["actual"] is not None and result["actual"] > result["reservation"]:
            raise MaintenanceBlocked()
        if not result["needed"]:
            return
        if not result["performed"]:
            raise InvalidMaintenanceResult()
        if result["segments"] < SEGMENT_THRESHOLD and not result["pending_merge"]:
            return
        report["counters_complete"] = False
    report["counters_complete"] = True
    raise MaintenanceStepLimit()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-steps", type=int, default=4)
    parser.add_argument("--health", default="fts-maintenance-health.json")
    args = parser.parse_args(argv)
    report = {"status": "blocked", "steps": 0, "performed": 0, "replayed": 0,
        "rows_written": 0, "reservations": 0, "counters_complete": False,
        "operation_ids": [], "maintenance": None, "anomalies": []}
    try:
        ids = operation_ids(os.environ, args.max_steps)
        endpoint = maintenance_url(os.environ.get("INGEST_URL"))
        token = os.environ.get("INGEST_TOKEN", "")
        if not token or any(ord(character) < 32 or ord(character) == 127 for character in token):
            raise ValueError("invalid maintenance credentials")
        with httpx.Client(timeout=25) as client:
            run_maintenance(client, endpoint, token, ids, report)
        report["status"] = "ready"
    except Exception as exc:
        report["anomalies"].append({"reason": type(exc).__name__})
    try:
        path = Path(args.health)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    except Exception as exc:
        report["status"] = "blocked"
        report["anomalies"].append({"reason": type(exc).__name__})
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
