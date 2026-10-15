from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

import pytest

from index_scrapers import cli
from index_scrapers.state import State


CAPTURED = "2026-10-14T13:50:43.771Z"


@pytest.fixture(autouse=True)
def supported_directory_sync_for_linux_apply_tests(monkeypatch):
    original = cli._sync_observation_directory
    if os.name == "nt":
        # Exercise the Linux application flow without claiming Windows durability.
        monkeypatch.setattr(cli, "_directory_fsync_supported", lambda: True)
        monkeypatch.setattr(cli, "_sync_observation_directory", lambda path: None)
    return original


@pytest.fixture
def envelope(listing):
    candidate = deepcopy(listing)
    candidate.pop("property_id")
    candidate.update(first_seen=CAPTURED, last_seen=CAPTURED, last_checked=CAPTURED)
    return {"status": "ready", "candidate": candidate, "reasons": [],
            "evidence": {"gallery_complete": None},
            "source": {"id": "10001", "observed_at": CAPTURED,
                       "source_card": {"id": "10001", "price_text": "1.250 €/mes"}}}


def save_envelopes(path, envelopes):
    raw = ("\n".join(json.dumps(item, ensure_ascii=False) for item in envelopes) + "\n").encode("utf-8")
    path.write_bytes(raw)
    return raw


def snapshots(directory):
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.rglob("*") if path.is_file()}


def tables(path):
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        return {table: list(db.execute(f"SELECT * FROM {table} ORDER BY 1"))
                for table in ("catalog", "queue", "progress")}


def arguments(state, source, report, *options):
    return ["import-observations", "--state", str(state), "--envelopes", str(source), "--report", str(report), *options]


def forbid_collection(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("observation import must not collect or deliver")

    for name in ("load_targets", "flush", "discover", "review", "initial_status", "restore", "snapshot"):
        monkeypatch.setattr(cli, name, unexpected)
    monkeypatch.setattr(cli.httpx, "Client", unexpected)
    for name in ("INGEST_URL", "INGEST_TOKEN", "GITHUB_REPOSITORY", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def test_preview_retains_every_envelope_without_state_files_or_network(tmp_path, listing, envelope, monkeypatch, capsys):
    state, source, report = (tmp_path / name for name in ("catalog.sqlite", "observations.jsonl", "preview.json"))
    with State(state) as db:
        db.observe(listing)
        db.progress("initial:idealista:28079:rent", {"url": "observed-page", "done": False})
    pending = {"status": "pending", "candidate": None, "reasons": ["first_seen_unknown"],
               "source": {"id": "20002", "source_card": {"id": "20002"}}, "evidence": {}}
    unknown = deepcopy(envelope)
    unknown["candidate"]["id"] = None
    unknown["candidate"]["property"]["municipality"] = None
    unknown["source"]["id"] = "30003"
    envelopes = [envelope, pending, unknown]
    raw = (json.dumps(envelope) + "\n\n" + json.dumps(pending) + "\n" + json.dumps(unknown) + "\n").encode()
    source.write_bytes(raw)
    health = tmp_path / "health.json"
    health.write_text("existing health", encoding="utf-8")
    (tmp_path / "progress.json").write_text("existing progress", encoding="utf-8")
    before, data_before = snapshots(tmp_path), tables(state)
    forbid_collection(monkeypatch)
    monkeypatch.setattr(cli, "State", lambda *args: pytest.fail("preview must not initialize State"))
    original_connect, calls = sqlite3.connect, []

    def immutable_connect(*args, **kwargs):
        calls.append((args, kwargs))
        assert args[0].endswith("?mode=ro&immutable=1")
        assert kwargs == {"uri": True}
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(cli.sqlite3, "connect", immutable_connect)
    assert cli.main(arguments(state, source, report, "--health", str(health), "--targets", "missing.json")) == 0
    assert len(calls) == 1
    monkeypatch.setattr(cli.sqlite3, "connect", original_connect)
    after = snapshots(tmp_path)
    assert set(after) - set(before) == {"preview.json"}
    assert {name: after[name] for name in before} == before
    assert tables(state) == data_before
    assert not state.with_name(state.name + "-wal").exists()
    assert not state.with_name(state.name + "-shm").exists()
    saved = json.loads(report.read_text(encoding="utf-8"))
    assert saved["applied"] is False
    assert saved["phase"] == "preview"
    assert saved["counts"]["input"] == 3
    assert saved["counts"]["planned"] == 1
    assert saved["counts"]["pending"] == 2
    assert [result["envelope"] for result in saved["results"]] == envelopes
    assert [result["input_line"] for result in saved["results"]] == [1, 3, 4]
    assert [result["envelope"]["source"]["id"] for result in saved["results"]] == ["10001", "20002", "30003"]
    assert saved["results"][2]["envelope"]["candidate"]["id"] is None
    assert saved["source"] == {"path": str(source.resolve()), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    output = json.loads(capsys.readouterr().out)
    assert output["counts"] == saved["counts"]
    assert "results" not in output


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("invalid", [b'{"secret": "do-not-leak"', b"[]\n", b"\xff\n",
                                     b'{"status":"pending","candidate":null,"reasons":[],"evidence":{},"source":NaN}\n',
                                     b'{"status":"ready","status":"pending","candidate":null,"reasons":[],"evidence":{},"source":{}}\n'])
def test_invalid_jsonl_fails_before_any_write(tmp_path, listing, envelope, monkeypatch, capsys, invalid, apply):
    state, source, report = (tmp_path / name for name in ("catalog.sqlite", "observations.jsonl", "report.json"))
    with State(state) as db:
        db.observe(listing)
    source.write_bytes(json.dumps(envelope).encode() + b"\n" + invalid)
    before = snapshots(tmp_path)
    forbid_collection(monkeypatch)
    monkeypatch.setattr(cli, "State", lambda *args: pytest.fail("invalid JSONL must precede State"))
    assert cli.main(arguments(state, source, report, *( ["--apply"] if apply else []))) == 1
    assert snapshots(tmp_path) == before
    output = capsys.readouterr().out
    assert "do-not-leak" not in output
    assert json.loads(output)["anomalies"][0]["line"] == 2


@pytest.mark.parametrize("field,value", [("status", "other"), ("candidate", "invalid"), ("reasons", [3]), ("evidence", [])])
def test_envelope_schema_is_validated_before_report(tmp_path, envelope, monkeypatch, field, value):
    state, source, report = (tmp_path / name for name in ("catalog.sqlite", "observations.jsonl", "report.json"))
    envelope[field] = value
    save_envelopes(source, [envelope])
    forbid_collection(monkeypatch)
    assert cli.main(arguments(state, source, report)) == 1
    assert not state.exists()
    assert not report.exists()


@pytest.mark.parametrize("bound", ["_MAX_OBSERVATION_BYTES", "_MAX_ENVELOPE_BYTES"])
def test_input_work_is_bounded_without_writing(tmp_path, envelope, monkeypatch, capsys, bound):
    source, report = tmp_path / "observations.jsonl", tmp_path / "report.json"
    save_envelopes(source, [envelope])
    monkeypatch.setattr(cli, bound, 20)
    assert cli.main(arguments(tmp_path / "catalog.sqlite", source, report)) == 1
    assert not report.exists()
    assert "too_large" in json.loads(capsys.readouterr().out)["anomalies"][0]["code"]


@pytest.mark.parametrize("apply", [False, True])
def test_missing_or_unrelated_state_is_never_initialized(tmp_path, envelope, monkeypatch, capsys, apply):
    source, report, state = (tmp_path / name for name in ("observations.jsonl", "report.json", "catalog.sqlite"))
    save_envelopes(source, [envelope])
    forbid_collection(monkeypatch)
    monkeypatch.setattr(cli, "State", lambda *args: pytest.fail("existing State is required"))
    options = ["--apply"] if apply else []
    assert cli.main(arguments(state, source, report, *options)) == 1
    assert json.loads(capsys.readouterr().out)["anomalies"][0]["code"] == "missing_state"
    assert not state.exists()
    with sqlite3.connect(state) as db:
        db.execute("CREATE TABLE unrelated(value TEXT)")
    before = snapshots(tmp_path)
    assert cli.main(arguments(state, source, report, *options)) == 1
    assert json.loads(capsys.readouterr().out)["anomalies"][0]["code"] == "invalid_state_schema"
    assert snapshots(tmp_path) == before


@pytest.mark.parametrize("destination", ["source", "state", "wal", "shm", "journal", "health", "existing"])
def test_report_cannot_overwrite_inputs_state_or_health(tmp_path, envelope, monkeypatch, destination):
    source, state, health = (tmp_path / name for name in ("observations.jsonl", "catalog.sqlite", "health.json"))
    save_envelopes(source, [envelope])
    with State(state):
        pass
    existing = tmp_path / "existing-report.json"
    existing.write_bytes(b"original report")
    paths = {"source": source, "state": state, "health": health, "existing": existing,
             "wal": Path(str(state) + "-wal"), "shm": Path(str(state) + "-shm"), "journal": Path(str(state) + "-journal")}
    before = snapshots(tmp_path)
    forbid_collection(monkeypatch)
    assert cli.main(arguments(state, source, paths[destination], "--health", str(health), "--apply")) == 1
    assert snapshots(tmp_path) == before


def test_preview_rejects_uncheckpointed_wal_without_touching_it(tmp_path, envelope, capsys):
    source, report, state = (tmp_path / name for name in ("observations.jsonl", "report.json", "catalog.sqlite"))
    save_envelopes(source, [envelope])
    with State(state) as db:
        db.observe(envelope["candidate"] | {"property_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV"})
        before = snapshots(tmp_path)
        assert cli.main(arguments(state, source, report)) == 1
        assert json.loads(capsys.readouterr().out)["anomalies"][0]["code"] == "state_requires_checkpoint"
        assert snapshots(tmp_path) == before


@pytest.mark.parametrize("operation", ["sale", "rent"])
def test_apply_retains_durable_originals_preserves_queue_and_replay_is_stable(tmp_path, listing, envelope, monkeypatch, operation):
    source, report, state = (tmp_path / name for name in ("observations.jsonl", "report.json", "catalog.sqlite"))
    listing["property"]["operation"] = operation
    envelope["candidate"]["property"]["operation"] = operation
    envelope["candidate"]["price"] = 0
    envelope["source"]["source_card"]["price_text"] = "0 €"
    pending = {"status": "pending", "candidate": None, "reasons": ["first_seen_unknown"],
               "source": {"id": "20002"}, "evidence": {}}
    raw = save_envelopes(source, [envelope, pending])
    with State(state) as db:
        db.observe(listing)
        db.progress("initial", {"done": False})
    before = tables(state)
    forbid_collection(monkeypatch)
    real_state, real_fsync, synced = cli.State, cli.os.fsync, []

    def fsync(descriptor):
        real_fsync(descriptor)
        synced.append(descriptor)

    def require_durable_originals(path):
        prepared = json.loads(report.read_text(encoding="utf-8"))
        retained = Path(prepared["retained_envelopes"]["path"])
        assert retained.read_bytes() == raw
        assert prepared["retained_envelopes"]["sha256"] == hashlib.sha256(raw).hexdigest()
        assert prepared["phase"] == "prepared"
        assert prepared["applied"] is False
        assert [result["envelope"] for result in prepared["results"]] == [envelope, pending]
        assert len(synced) >= 2
        return real_state(path)

    monkeypatch.setattr(cli.os, "fsync", fsync)
    monkeypatch.setattr(cli, "State", require_durable_originals)
    assert cli.main(arguments(state, source, report, "--apply")) == 0
    saved = json.loads(report.read_text(encoding="utf-8"))
    assert saved["applied"] is True
    assert saved["phase"] == "applied"
    assert saved["counts"]["staged"] == saved["counts"]["pending"] == 1
    assert [result["envelope"] for result in saved["results"]] == [envelope, pending]
    with real_state(state) as db:
        assert db.catalog()[0]["price"] == 0
        assert db.catalog()[0]["first_seen"] == listing["first_seen"]
        assert db.catalog()[0]["last_checked"] == CAPTURED
        assert db.pending_count() == 2
    after = tables(state)
    assert after["queue"][0] == before["queue"][0]
    assert after["progress"] == before["progress"]
    monkeypatch.setattr(cli, "State", real_state)
    repeated = tmp_path / "repeat.json"
    assert cli.main(arguments(state, source, repeated, "--apply")) == 0
    replay = json.loads(repeated.read_text(encoding="utf-8"))
    assert replay["counts"]["staged"] == 0
    assert replay["counts"]["unchanged"] == replay["counts"]["pending"] == 1
    assert tables(state) == after
    assert source.read_bytes() == raw
    assert not (tmp_path / "health.json").exists()


def test_apply_recomputes_gate_against_state_after_preview(tmp_path, listing, envelope, monkeypatch):
    source, report, state = (tmp_path / name for name in ("observations.jsonl", "report.json", "catalog.sqlite"))
    save_envelopes(source, [envelope])
    with State(state) as db:
        db.observe(listing)
    real_state = cli.State
    newer = deepcopy(listing)
    newer.update(price=999, last_seen="2026-10-15T12:00:00Z", last_checked="2026-10-15T12:00:00Z")

    def state_changed_since_preview(path):
        prepared = json.loads(report.read_text(encoding="utf-8"))
        assert prepared["counts"]["planned"] == 1
        with real_state(path) as db:
            db.observe(newer)
        return real_state(path)

    forbid_collection(monkeypatch)
    monkeypatch.setattr(cli, "State", state_changed_since_preview)
    assert cli.main(arguments(state, source, report, "--apply")) == 0
    saved = json.loads(report.read_text(encoding="utf-8"))
    assert saved["counts"]["ignored"] == 1
    assert saved["counts"]["staged"] == 0
    assert saved["results"][0]["gate_reasons"] == ["stale_capture"]
    with real_state(state) as db:
        assert db.catalog()[0] == newer
        assert db.pending_count() == 2


def test_apply_locks_catalog_decision_against_competing_sqlite_writer(tmp_path, listing, envelope, monkeypatch):
    source, report, state = (tmp_path / name for name in ("observations.jsonl", "report.json", "catalog.sqlite"))
    save_envelopes(source, [envelope])
    with State(state) as db:
        db.observe(listing)
    original_import = cli.import_observations

    def import_with_competing_writer(writer, envelopes, *, apply):
        assert apply is True
        assert writer.catalog() == [listing]
        with sqlite3.connect(state, timeout=0) as competitor:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competitor.execute("UPDATE catalog SET misses=1")
        return original_import(writer, envelopes, apply=apply)

    forbid_collection(monkeypatch)
    monkeypatch.setattr(cli, "import_observations", import_with_competing_writer)
    assert cli.main(arguments(state, source, report, "--apply")) == 0
    saved = json.loads(report.read_text(encoding="utf-8"))
    assert saved["counts"]["staged"] == 0
    assert saved["counts"]["unchanged"] == 1
    with State(state) as db:
        assert db.pending_count() == 1
        assert list(db.db.execute("SELECT misses FROM catalog"))[0][0] == 0


@pytest.mark.parametrize("outcome", ["pending", "ignored", "rejected"])
def test_apply_releases_lock_for_decisions_without_state_writes(tmp_path, listing, envelope, monkeypatch, outcome):
    source, report, state = (tmp_path / name for name in ("observations.jsonl", "report.json", "catalog.sqlite"))
    if outcome == "pending":
        envelope.update(status="pending", reasons=["first_seen_unknown"])
    elif outcome == "ignored":
        listing.update(last_seen="2026-10-15T12:00:00Z", last_checked="2026-10-15T12:00:00Z")
    else:
        envelope["candidate"]["property"]["operation"] = "sale"
    save_envelopes(source, [envelope])
    with State(state) as db:
        db.observe(listing)
        db.progress("existing", {"done": False})
    before = tables(state)
    original_import = cli.import_observations

    def import_with_lock_check(writer, envelopes, *, apply):
        writer.catalog()
        with sqlite3.connect(state, timeout=0) as competitor:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competitor.execute("UPDATE progress SET value='{}'")
        return original_import(writer, envelopes, apply=apply)

    forbid_collection(monkeypatch)
    monkeypatch.setattr(cli, "import_observations", import_with_lock_check)
    assert cli.main(arguments(state, source, report, "--apply")) == 0
    assert json.loads(report.read_text(encoding="utf-8"))["counts"][outcome] == 1
    assert tables(state) == before
    with sqlite3.connect(state, timeout=0) as competitor:
        competitor.execute("INSERT INTO progress(key,value) VALUES('lock-released','{}')")


def test_failure_before_durable_report_never_applies_or_leaks_error(tmp_path, envelope, monkeypatch, capsys):
    source, report, state = (tmp_path / name for name in ("observations.jsonl", "report.json", "catalog.sqlite"))
    save_envelopes(source, [envelope])
    with State(state):
        pass
    before = tables(state)
    secret = "secret-token-that-must-not-appear"

    def failure(*args, **kwargs):
        raise PermissionError("Authorization: Bearer " + secret)

    forbid_collection(monkeypatch)
    monkeypatch.setattr(cli, "_save_observation_report", failure)
    monkeypatch.setattr(cli, "State", lambda *args: pytest.fail("report must be durable before apply"))
    assert cli.main(arguments(state, source, report, "--apply")) == 1
    output = capsys.readouterr().out
    assert secret not in output
    assert "Authorization" not in output
    assert json.loads(output)["anomalies"] == [{"portal": "runner", "reason": "PermissionError"}]
    assert tables(state) == before
    assert not report.exists()


def test_apply_failure_preserves_prepared_originals_and_sanitizes_exception(tmp_path, envelope, monkeypatch, capsys):
    source, report, state = (tmp_path / name for name in ("observations.jsonl", "report.json", "catalog.sqlite"))
    raw = save_envelopes(source, [envelope])
    with State(state):
        pass
    secret = "secret-token-that-must-not-appear"

    def failure(*args, **kwargs):
        raise RuntimeError("Authorization: Bearer " + secret)

    forbid_collection(monkeypatch)
    monkeypatch.setattr(cli, "import_observations", failure)
    assert cli.main(arguments(state, source, report, "--apply")) == 1
    saved = json.loads(report.read_text(encoding="utf-8"))
    assert saved["phase"] == "failed"
    assert saved["applied"] is None
    assert saved["results"][0]["envelope"] == envelope
    assert Path(saved["retained_envelopes"]["path"]).read_bytes() == raw
    assert saved["anomalies"] == [{"portal": "runner", "reason": "RuntimeError"}]
    assert secret not in capsys.readouterr().out
    assert secret not in report.read_text(encoding="utf-8")


def test_report_sync_failure_closes_and_removes_temporary_file(tmp_path, envelope, monkeypatch):
    source, report, state = (tmp_path / name for name in ("observations.jsonl", "report.json", "catalog.sqlite"))
    save_envelopes(source, [envelope])
    with State(state):
        pass
    real_fsync, calls = cli.os.fsync, []

    def fail_final_report_sync(descriptor):
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            calls.append(descriptor)
            if len(calls) == 3:
                raise OSError("simulated final report sync failure")
        return real_fsync(descriptor)

    forbid_collection(monkeypatch)
    monkeypatch.setattr(cli.os, "fsync", fail_final_report_sync)
    assert cli.main(arguments(state, source, report, "--apply")) == 1
    saved = json.loads(report.read_text(encoding="utf-8"))
    assert saved["phase"] == "failed"
    assert saved["applied"] is None
    assert saved["anomalies"] == [{"portal": "runner", "reason": "OSError"}]
    assert not list(tmp_path.glob("report.json.*.tmp"))


def test_directory_fsync_syscall_order_precedes_state_and_follows_replace(
        tmp_path, envelope, monkeypatch, supported_directory_sync_for_linux_apply_tests):
    source, report, state = (tmp_path / name for name in ("observations.jsonl", "report.json", "catalog.sqlite"))
    save_envelopes(source, [envelope])
    with State(state):
        pass
    events, directories = [], {}
    real_open, real_close, real_fsync = os.open, os.close, os.fsync
    real_replace, real_state = os.replace, cli.State
    directory_flag = getattr(os, "O_DIRECTORY", 1 << 29)

    def open_directory(path, flags, *args, **kwargs):
        if flags & directory_flag:
            descriptor = 1_000_000 + len(events)
            directories[descriptor] = Path(path)
            events.append(("open-directory", Path(path)))
            return descriptor
        return real_open(path, flags, *args, **kwargs)

    def fsync(descriptor):
        if descriptor in directories:
            events.append(("directory-fsync", directories[descriptor]))
        else:
            events.append(("file-fsync",))
            real_fsync(descriptor)

    def close(descriptor):
        if descriptor in directories:
            events.append(("close-directory", directories.pop(descriptor)))
        else:
            real_close(descriptor)

    def replace(source_path, destination):
        events.append(("replace-report",))
        real_replace(source_path, destination)

    def open_state(path):
        events.append(("state",))
        return real_state(path)

    forbid_collection(monkeypatch)
    monkeypatch.setattr(cli, "_directory_fsync_supported", lambda: True)
    monkeypatch.setattr(cli, "_sync_observation_directory", supported_directory_sync_for_linux_apply_tests)
    monkeypatch.setattr(cli.os, "O_DIRECTORY", directory_flag, raising=False)
    monkeypatch.setattr(cli.os, "open", open_directory)
    monkeypatch.setattr(cli.os, "fsync", fsync)
    monkeypatch.setattr(cli.os, "close", close)
    monkeypatch.setattr(cli.os, "replace", replace)
    monkeypatch.setattr(cli, "State", open_state)
    assert cli.main(arguments(state, source, report, "--apply")) == 0
    sync_directory = [("open-directory", tmp_path), ("directory-fsync", tmp_path), ("close-directory", tmp_path)]
    assert events == [("file-fsync",), *sync_directory, ("file-fsync",), *sync_directory,
                      ("state",), ("file-fsync",), ("replace-report",), *sync_directory]
    assert not directories


def test_new_report_directories_are_synced_before_application(tmp_path, envelope, monkeypatch):
    source, state = tmp_path / "observations.jsonl", tmp_path / "catalog.sqlite"
    report = tmp_path / "new" / "nested" / "report.json"
    save_envelopes(source, [envelope])
    with State(state):
        pass
    synced, real_state = [], cli.State

    def open_state(path):
        assert synced == [tmp_path, tmp_path / "new", report.parent, report.parent]
        return real_state(path)

    forbid_collection(monkeypatch)
    monkeypatch.setattr(cli, "_sync_observation_directory", synced.append)
    monkeypatch.setattr(cli, "State", open_state)
    assert cli.main(arguments(state, source, report, "--apply")) == 0
    assert synced[-1] == report.parent


@pytest.mark.parametrize("failure", ["new_parent", "originals", "prepared_report"])
def test_directory_sync_failure_prevents_application(tmp_path, envelope, monkeypatch, capsys, failure):
    source, state = tmp_path / "observations.jsonl", tmp_path / "catalog.sqlite"
    report = tmp_path / "report.json"
    if failure == "new_parent":
        report = tmp_path / "new" / "report.json"
    raw = save_envelopes(source, [envelope])
    with State(state):
        pass
    before, state_hash = tables(state), hashlib.sha256(state.read_bytes()).hexdigest()
    calls, fail_at = [], 2 if failure == "prepared_report" else 1

    def failing_sync(path):
        calls.append(path)
        if len(calls) == fail_at:
            raise OSError("Authorization: Bearer secret-directory-token")

    forbid_collection(monkeypatch)
    monkeypatch.setattr(cli, "_sync_observation_directory", failing_sync)
    monkeypatch.setattr(cli, "State", lambda *args: pytest.fail("directory fsync must precede State"))
    assert cli.main(arguments(state, source, report, "--apply")) == 1
    output = capsys.readouterr().out
    summary = json.loads(output)
    assert summary["phase"] == "failed"
    assert summary["applied"] is False
    assert summary["application_started"] is False
    assert summary["anomalies"] == [{"portal": "runner", "reason": "OSError"}]
    assert "secret-directory-token" not in output
    assert tables(state) == before
    assert hashlib.sha256(state.read_bytes()).hexdigest() == state_hash
    if failure == "prepared_report":
        saved = json.loads(report.read_text(encoding="utf-8"))
        assert saved["phase"] == "failed"
        assert saved["applied"] is False
        assert saved["application_started"] is False
        assert saved["results"][0]["envelope"] == envelope
        assert Path(saved["retained_envelopes"]["path"]).read_bytes() == raw
        assert "secret-directory-token" not in report.read_text(encoding="utf-8")
    else:
        assert not report.exists()


def test_directory_sync_failure_after_apply_marks_partial_result(tmp_path, envelope, monkeypatch):
    source, report, state = (tmp_path / name for name in ("observations.jsonl", "report.json", "catalog.sqlite"))
    save_envelopes(source, [envelope])
    with State(state):
        pass
    calls = []

    def failing_final_sync(path):
        calls.append(path)
        if len(calls) == 3:
            raise OSError("simulated final directory sync failure")

    forbid_collection(monkeypatch)
    monkeypatch.setattr(cli, "_sync_observation_directory", failing_final_sync)
    assert cli.main(arguments(state, source, report, "--apply")) == 1
    saved = json.loads(report.read_text(encoding="utf-8"))
    assert saved["phase"] == "failed"
    assert saved["applied"] is None
    assert saved["application_started"] is True
    assert saved["counts"]["staged"] == 1
    assert saved["anomalies"] == [{"portal": "runner", "reason": "OSError"}]
    with State(state) as db:
        assert db.pending_count() == 1
    assert len(calls) == 4


def test_unsupported_directory_fsync_blocks_apply_but_allows_preview(tmp_path, envelope, monkeypatch, capsys):
    source, state = tmp_path / "observations.jsonl", tmp_path / "catalog.sqlite"
    report = tmp_path / "new" / "report.json"
    save_envelopes(source, [envelope])
    with State(state):
        pass
    before = snapshots(tmp_path)
    forbid_collection(monkeypatch)
    monkeypatch.setattr(cli, "_directory_fsync_supported", lambda: False)
    monkeypatch.setattr(cli, "State", lambda *args: pytest.fail("unsupported directory durability must precede State"))
    assert cli.main(arguments(state, source, report, "--apply")) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["application_started"] is False
    assert summary["anomalies"][0]["code"] == "directory_fsync_unsupported"
    assert snapshots(tmp_path) == before
    assert cli.main(arguments(state, source, report)) == 0
    assert json.loads(report.read_text(encoding="utf-8"))["durability"]["directory_fsync_supported"] is False


def test_existing_retained_originals_cannot_be_replaced(tmp_path, envelope):
    source, report, state = (tmp_path / name for name in ("observations.jsonl", "report.json", "catalog.sqlite"))
    save_envelopes(source, [envelope])
    retained = report.with_name(report.name + ".envelopes.jsonl")
    retained.write_bytes(b"original observations")
    before = snapshots(tmp_path)
    assert cli.main(arguments(state, source, report, "--apply")) == 1
    assert snapshots(tmp_path) == before


def test_required_arguments_and_import_flags_do_not_change_other_modes(tmp_path, monkeypatch):
    forbid_collection(monkeypatch)
    for args in (["import-observations"], ["import-observations", "--envelopes", "source.jsonl"], ["health", "--apply"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(args)
        assert exc.value.code == 2
