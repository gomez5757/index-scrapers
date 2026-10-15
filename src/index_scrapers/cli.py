import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

import httpx

from .artifacts import restore, snapshot
from .delivery import flush
from .observation_importer import import_observations, plan_observations
from .runner import discover, initial_status, load_targets, review
from .state import State


_MAX_OBSERVATION_BYTES = 64 * 1024 * 1024
_MAX_ENVELOPE_BYTES = 1024 * 1024


class ObservationInputError(ValueError):
    def __init__(self, code, line=None):
        super().__init__(code)
        self.code = code
        self.line = line


class _ObservationWriter:
    def __init__(self, state):
        self.state = state

    def catalog(self):
        # Keep each catalogue decision and its State write under the same lock.
        if not self.state.db.in_transaction:
            self.state.db.execute("BEGIN IMMEDIATE")
        return self.state.catalog()

    def observe(self, listing):
        return self.state.observe(listing)


def _reject_json_constant(value):
    raise ValueError("nonfinite JSON number")


def _observation_json_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _read_envelopes(path):
    with path.open("rb") as source:
        raw = source.read(_MAX_OBSERVATION_BYTES + 1)
    if len(raw) > _MAX_OBSERVATION_BYTES:
        raise ObservationInputError("envelopes_too_large")
    envelopes, lines = [], []
    for number, line in enumerate(raw.split(b"\n"), 1):
        if len(line) > _MAX_ENVELOPE_BYTES:
            raise ObservationInputError("envelope_too_large", number)
        if not line.strip():
            continue
        try:
            envelope = json.loads(line.decode("utf-8"), parse_constant=_reject_json_constant,
                                  object_pairs_hook=_observation_json_object)
        except (ValueError, UnicodeError, RecursionError):
            raise ObservationInputError("invalid_jsonl", number) from None
        if (not isinstance(envelope, dict) or
                not {"status", "candidate", "reasons", "evidence", "source"} <= envelope.keys() or
                envelope["status"] not in ("ready", "pending") or
                envelope["candidate"] is not None and not isinstance(envelope["candidate"], dict) or
                not isinstance(envelope["reasons"], list) or
                not all(isinstance(reason, str) for reason in envelope["reasons"]) or
                not isinstance(envelope["evidence"], dict)):
            raise ObservationInputError("invalid_envelope_schema", number)
        envelopes.append(envelope)
        lines.append(number)
    return raw, envelopes, lines


def _observation_catalog(path):
    if not path.is_file():
        raise ObservationInputError("missing_state")
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ObservationInputError("state_too_large")
    wal = path.with_name(path.name + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise ObservationInputError("state_requires_checkpoint")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        required = {"catalog": {"id", "payload", "fingerprint", "misses"},
                    "queue": {"seq", "id", "payload"}, "progress": {"key", "value"}}
        for table, columns in required.items():
            if not columns <= {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
                raise ObservationInputError("invalid_state_schema")
        catalog = [json.loads(row[0]) for row in db.execute("SELECT payload FROM catalog ORDER BY id")]
        if not all(isinstance(record, dict) for record in catalog):
            raise ObservationInputError("invalid_state_catalog")
        return catalog


def _directory_fsync_supported():
    return os.name == "posix" and hasattr(os, "O_DIRECTORY")


def _sync_observation_directory(path):
    if not _directory_fsync_supported():
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _prepare_observation_directory(path):
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    for directory in reversed(missing):
        _sync_observation_directory(directory.parent)


def _save_observation_report(path, report, *, create=False, on_create=None):
    data = (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if create:
        with path.open("xb") as destination:
            if on_create is not None:
                on_create()
            destination.write(data)
            destination.flush()
            os.fsync(destination.fileno())
        _sync_observation_directory(path.parent)
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as destination:
            temporary = Path(destination.name)
            destination.write(data)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
        _sync_observation_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _import_observation_cli(args):
    report = {"mode": args.mode, "apply_requested": args.apply, "application_started": False,
              "applied": False, "anomalies": []}
    owned_report = False

    def mark_owned_report():
        nonlocal owned_report
        owned_report = True

    try:
        source, path, state_path = (Path(value).resolve() for value in (args.envelopes, args.report, args.state))
        retained = path.with_name(path.name + ".envelopes.jsonl")
        protected = {source, state_path, Path(args.health).resolve(),
                     *(state_path.with_name(state_path.name + suffix) for suffix in ("-wal", "-shm", "-journal"))}
        if path in protected or args.apply and retained in protected:
            raise ObservationInputError("report_must_be_separate")
        if path.exists() or args.apply and retained.exists():
            raise ObservationInputError("report_already_exists")
        raw, envelopes, lines = _read_envelopes(source)
        catalog = _observation_catalog(state_path)
        digest = hashlib.sha256(raw).hexdigest()
        report.update(plan_observations(envelopes, catalog))
        for result, line in zip(report["results"], lines):
            result["input_line"] = line
        report.update(phase="prepared" if args.apply else "preview",
                      source={"path": str(source), "sha256": digest, "bytes": len(raw)},
                      durability={"directory_fsync_supported": _directory_fsync_supported()})
        if args.apply and not report["durability"]["directory_fsync_supported"]:
            raise ObservationInputError("directory_fsync_unsupported")
        _prepare_observation_directory(path)
        if args.apply:
            with retained.open("xb") as destination:
                destination.write(raw)
                destination.flush()
                os.fsync(destination.fileno())
            report["retained_envelopes"] = {"path": str(retained), "sha256": digest}
            _sync_observation_directory(retained.parent)
        _save_observation_report(path, report, create=True, on_create=mark_owned_report)
        if args.apply:
            if not state_path.is_file():
                raise ObservationInputError("missing_state")
            report["application_started"] = True
            with State(state_path) as state:
                with state.db:
                    report.update(import_observations(_ObservationWriter(state), envelopes, apply=True))
            for result, line in zip(report["results"], lines):
                result["input_line"] = line
            report["phase"] = "applied"
            _save_observation_report(path, report)
    except Exception as exc:
        anomaly = {"portal": "runner", "reason": type(exc).__name__}
        if isinstance(exc, ObservationInputError):
            anomaly["code"] = exc.code
            if exc.line is not None:
                anomaly["line"] = exc.line
        report["anomalies"].append(anomaly)
        report.update(phase="failed", applied=None if report["application_started"] else False)
        if owned_report:
            try:
                _save_observation_report(path, report)
            except Exception as report_error:
                report["anomalies"].append({"portal": "report", "reason": type(report_error).__name__})
    summary = {key: report[key] for key in ("mode", "apply_requested", "application_started", "applied", "phase", "counts", "anomalies") if key in report}
    if owned_report:
        summary["report"] = str(path)
        summary["source_sha256"] = report["source"]["sha256"]
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if report["anomalies"] else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Index scrapers: durable HTTP collection")
    parser.add_argument("mode", choices=["discover", "initial", "prices", "withdrawals", "health", "restore", "snapshot", "deliver", "import-observations"])
    parser.add_argument("--operation", choices=["rent", "sale"])
    parser.add_argument("--state", default="state/catalog.sqlite")
    parser.add_argument("--targets", default="config/targets.json")
    parser.add_argument("--municipalities", default="data/municipalities.csv")
    parser.add_argument("--health", default="health.json")
    parser.add_argument("--snapshot-dir", default="state/artifact")
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--no-delivery", action="store_true")
    parser.add_argument("--continue-initial", action="store_true", help="Resume eligible initial collection before regular discovery")
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--initial-max-pages", type=int, default=36, help="Maximum pages for automatic initial continuation (default: 36)")
    parser.add_argument("--limit", type=int, default=100, help="Maximum reviewed listings or explicitly delivered items (default: 100)")
    parser.add_argument("--envelopes", help="Saved observation envelopes in JSONL format")
    parser.add_argument("--report", help="New, separate full observation report")
    parser.add_argument("--apply", action="store_true", help="Explicitly stage eligible observations in existing local state")
    args = parser.parse_args(argv)
    if args.mode == "import-observations":
        if not args.envelopes or not args.report:
            parser.error("import-observations requires --envelopes and --report")
        return _import_observation_cli(args)
    if args.envelopes or args.report or args.apply:
        parser.error("--envelopes, --report and --apply require import-observations")
    report = {"mode": args.mode, "anomalies": []}
    try:
        if args.mode not in {"restore", "snapshot"} and (args.max_pages < 1 or args.initial_max_pages < 1 or args.limit < 1):
            raise ValueError("limits must be positive")
        with httpx.Client(timeout=25, headers={"User-Agent": "Index/0.1 (+authorized portal data collection)"}) as client:
            if args.mode == "restore":
                result = restore(client, os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"], args.state, args.initialize, os.environ.get("DEFAULT_BRANCH", "main"))
                print(json.dumps({"restored": result}))
                return 0
            if args.mode == "snapshot":
                if not Path(args.state).is_file():
                    raise ValueError("missing state; cannot publish empty snapshot")
                with State(args.state) as state:
                    snapshot(state, args.snapshot_dir)
                print(json.dumps({"snapshot": "saved"}))
                return 0
            targets = load_targets(args.targets, args.municipalities) if args.mode != "deliver" else []
            if args.mode == "deliver" and not Path(args.state).is_file():
                raise ValueError("missing durable state before delivery")
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            with State(args.state) as state:
                if args.mode in {"discover", "initial", "health"}:
                    all_targets = targets
                    continuing_initial = args.mode == "discover" and args.continue_initial and initial_status(state, all_targets, now)["eligible"] > 0
                    if not continuing_initial:
                        targets = [target for target in targets if args.operation is None or target["operation"] == args.operation]
                    if not targets:
                        raise ValueError("no configured targets for this operation")
                    page_limit = args.initial_max_pages if continuing_initial else args.max_pages
                    report.update(discover(state, client, targets, now, max_pages=1 if args.mode == "health" else page_limit, hash_images=args.mode != "health", resume=args.mode == "initial" or continuing_initial, read_only=args.mode == "health"))
                    if args.mode == "discover" and args.continue_initial:
                        report.setdefault("initial", {}).update(initial_status(state, all_targets, now))
                elif args.mode != "deliver":
                    report.update(review(state, client, args.operation, now, mode=args.mode, limit=args.limit))
                if not args.no_delivery and args.mode != "health":
                    delivery_options = {"max_items": args.limit} if args.mode == "deliver" else {}
                    report["delivery"] = flush(state, client, os.environ["INGEST_URL"], os.environ["INGEST_TOKEN"], **delivery_options)
    except Exception as exc:
        report["anomalies"].append({"portal": "runner", "reason": type(exc).__name__})
    path = Path(args.health)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    print(json.dumps(report, ensure_ascii=True))
    return 1 if report["anomalies"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
