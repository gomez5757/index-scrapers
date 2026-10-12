from pathlib import Path
from zipfile import ZipFile
import gzip
import hashlib
import io
import json
import httpx
import pytest


def archive(state_path):
    data = state_path.read_bytes()
    stream = io.BytesIO()
    with ZipFile(stream, "w") as target:
        target.writestr("catalog.sqlite.gz", gzip.compress(data))
        target.writestr("manifest.json", json.dumps({"schema": 1, "sha256": hashlib.sha256(data).hexdigest()}))
    return stream.getvalue()


def test_artifact_restores_catalog_pending_and_checks(tmp_path, listing):
    from index_scrapers.artifacts import restore
    from index_scrapers.state import State
    with State(tmp_path / "source.sqlite") as state:
        state.observe(listing)
        state.check(listing["id"], 404, "2026-10-12T13:00:00Z")
        state.backup(tmp_path / "backup.sqlite")
    zipped = archive(tmp_path / "backup.sqlite")
    def endpoint(request):
        if request.url.path.endswith("/artifacts"):
            return httpx.Response(200, json={"artifacts": [{"id": 12, "name": "index-state-1", "expired": False, "created_at": "2026-10-12T13:00:00Z", "workflow_run": {"head_branch": "main", "repository_id": 1, "head_repository_id": 1}}]})
        assert request.url.path.endswith("/12/zip")
        return httpx.Response(200, content=zipped)
    with httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        restore(client, "owner/repo", "github-test-token", tmp_path / "restored.sqlite")
    with State(tmp_path / "restored.sqlite") as state:
        assert len(state.pending()) == 1
        assert state.check(listing["id"], 410, "2026-10-12T14:00:00Z") is True


def test_missing_artifact_never_silently_initializes_empty_state(tmp_path):
    from index_scrapers.artifacts import restore, StateUnavailable
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"artifacts": []}))) as client:
        with pytest.raises(StateUnavailable):
            restore(client, "owner/repo", "github-test-token", tmp_path / "restored.sqlite")
        assert not (tmp_path / "restored.sqlite").exists()


def test_initialization_requires_explicit_flag(tmp_path):
    from index_scrapers.artifacts import restore
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"artifacts": []}))) as client:
        assert restore(client, "owner/repo", "github-test-token", tmp_path / "state.sqlite", initialize=True) == "initialized"
        assert (tmp_path / "state.sqlite").exists()


def test_corrupt_newest_artifact_is_blocker_not_rollback(tmp_path):
    from index_scrapers.artifacts import restore, StateUnavailable
    def endpoint(request):
        if request.url.path.endswith("/artifacts"):
            return httpx.Response(200, json={"artifacts": [{"id": 12, "name": "index-state-1", "expired": False, "created_at": "2026-10-12T13:00:00Z", "workflow_run": {"head_branch": "main", "repository_id": 1, "head_repository_id": 1}}]})
        return httpx.Response(200, content=b"corrupt")
    with httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        with pytest.raises(StateUnavailable):
            restore(client, "owner/repo", "github-test-token", tmp_path / "state.sqlite")
        assert not (tmp_path / "state.sqlite").exists()


def test_snapshot_is_consistent_and_contains_no_images(tmp_path, listing):
    from index_scrapers.artifacts import snapshot
    from index_scrapers.state import State
    with State(tmp_path / "source.sqlite") as state:
        state.observe(listing)
        snapshot(state, tmp_path / "artifact")
    manifest = json.loads((tmp_path / "artifact/manifest.json").read_text())
    assert manifest["sha256"] == hashlib.sha256(gzip.decompress((tmp_path / "artifact/catalog.sqlite.gz").read_bytes())).hexdigest()
    assert {path.name for path in (tmp_path / "artifact").iterdir()} == {"catalog.sqlite.gz", "manifest.json"}


def test_state_artifact_from_pull_request_branch_is_never_restored(tmp_path):
    from index_scrapers.artifacts import restore, StateUnavailable
    artifact = {"id": 12, "name": "index-state-pr", "expired": False, "created_at": "2026-10-12T13:00:00Z",
        "workflow_run": {"head_branch": "feature", "repository_id": 1, "head_repository_id": 2}}
    def endpoint(request):
        assert request.url.path.endswith("/artifacts")
        return httpx.Response(200, json={"artifacts": [artifact]})
    with httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        with pytest.raises(StateUnavailable):
            restore(client, "owner/repo", "github-test-token", tmp_path / "state.sqlite")
        assert not (tmp_path / "state.sqlite").exists()
