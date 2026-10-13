from pathlib import Path
from zipfile import ZipFile
import gzip
import hashlib
import io
import json
import shutil
import subprocess
import textwrap
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


def state_artifact(identifier, created_at, run=38298310419, attempt=1, phase="final", **changes):
    artifact = {"id": identifier, "name": f"index-state-{run}-{attempt}-{phase}",
        "expired": False, "created_at": created_at,
        "workflow_run": {"id": run, "head_branch": "main", "repository_id": 1, "head_repository_id": 1}}
    artifact.update(changes)
    return artifact


def prune_snapshots(artifacts, error=None):
    workflow = (Path(__file__).parents[1] / ".github/workflows/run.yml").read_text(encoding="utf-8")
    step = workflow.split("      - name: Keep three snapshots after successful upload\n", 1)[1]
    script = textwrap.dedent(step.split("          script: |\n", 1)[1].split("      - name:", 1)[0])
    node = shutil.which("node")
    assert node, "Node.js is required to execute the workflow's pruning script"
    harness = """
        const fs = require('fs');
        const {script, artifacts} = JSON.parse(fs.readFileSync(0, 'utf8'));
        const deleted = [];
        const listArtifactsForRepo = () => {};
        const github = {
          paginate: async (endpoint, params) => {
            if (endpoint !== listArtifactsForRepo || params.owner !== 'owner'
                || params.repo !== 'repo' || params.per_page !== 100) throw new Error('invalid listing');
            return artifacts;
          },
          rest: {actions: {listArtifactsForRepo,
            deleteArtifact: async params => {
              if (params.owner !== 'owner' || params.repo !== 'repo') throw new Error('invalid deletion');
              deleted.push(params.artifact_id);
            }}}
        };
        const context = {repo: {owner: 'owner', repo: 'repo'},
          payload: {repository: {id: 1, default_branch: 'main'}}};
        const AsyncFunction = Object.getPrototypeOf(async function() {}).constructor;
        new AsyncFunction('github', 'context', script)(github, context)
          .then(() => process.stdout.write(JSON.stringify(deleted)))
          .catch(error => {process.stdout.write(JSON.stringify(deleted)); console.error(error); process.exitCode = 1;});
    """
    result = subprocess.run([node, "-e", harness], input=json.dumps({"script": script, "artifacts": artifacts}),
        capture_output=True, text=True, check=False)
    if error:
        assert result.returncode != 0
        assert error in result.stderr
        assert json.loads(result.stdout) == []
        return
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("final_created_at", ["2026-10-13T01:04:00Z", "2026-10-13T01:03:00Z"])
def test_restore_preserves_final_ack_when_artifact_ids_go_backwards(tmp_path, listing, final_created_at):
    from index_scrapers.artifacts import restore
    from index_scrapers.state import State
    with State(tmp_path / "source.sqlite") as state:
        state.observe(listing)
        state.backup(tmp_path / "pre.sqlite")
        state.acknowledge(state.pending(), [listing["id"]])
        state.backup(tmp_path / "final.sqlite")
    pre = state_artifact(11770091838, "2026-10-13T01:03:00Z", phase="pre")
    final = state_artifact(11769901956, final_created_at)
    snapshots = {pre["id"]: archive(tmp_path / "pre.sqlite"), final["id"]: archive(tmp_path / "final.sqlite")}
    def endpoint(request):
        if request.url.path.endswith("/artifacts"):
            return httpx.Response(200, json={"artifacts": [pre, final]})
        identifier = int(request.url.path.split("/")[-2])
        return httpx.Response(200, content=snapshots[identifier])
    with httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        assert restore(client, "owner/repo", "github-test-token", tmp_path / "restored.sqlite") == str(final["id"])
    with State(tmp_path / "restored.sqlite") as state:
        assert state.pending() == []


def test_prune_keeps_latest_final_when_artifact_ids_go_backwards():
    oldest = state_artifact(11770091840, "2026-10-13T01:00:00Z", run=38298310417)
    older = state_artifact(11770091839, "2026-10-13T01:01:00Z", run=38298310418)
    pre = state_artifact(11770091838, "2026-10-13T01:03:00Z", phase="pre")
    final = state_artifact(11769901956, "2026-10-13T01:04:00Z")
    assert prune_snapshots([oldest, older, pre, final]) == [oldest["id"]]


def test_prune_keeps_final_before_pre_of_same_run_and_attempt_at_retention_boundary():
    pre = state_artifact(11770091838, "2026-10-13T01:03:00Z", phase="pre")
    final = state_artifact(11769901956, pre["created_at"])
    newer = state_artifact(11770091839, "2026-10-13T01:04:00Z", run=38298310420)
    newest = state_artifact(11770091840, "2026-10-13T01:05:00Z", run=38298310421)
    assert prune_snapshots([pre, final, newer, newest]) == [pre["id"]]


@pytest.mark.parametrize("failure", ["expired", "corrupt"])
def test_latest_final_failure_never_rolls_back_to_pre_at_same_timestamp(tmp_path, failure):
    from index_scrapers.artifacts import restore, StateUnavailable
    pre = state_artifact(11770091838, "2026-10-13T01:03:00Z", phase="pre")
    final = state_artifact(11769901956, pre["created_at"], expired=failure == "expired")
    downloads = []
    def endpoint(request):
        if request.url.path.endswith("/artifacts"):
            return httpx.Response(200, json={"artifacts": [pre, final]})
        downloads.append(int(request.url.path.split("/")[-2]))
        return httpx.Response(200, content=b"corrupt")
    with httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        with pytest.raises(StateUnavailable):
            restore(client, "owner/repo", "github-test-token", tmp_path / "restored.sqlite")
    assert downloads == ([] if failure == "expired" else [final["id"]])
    assert not (tmp_path / "restored.sqlite").exists()


@pytest.mark.parametrize("changes", [{"run": 38298310420}, {"attempt": 2}, {"name": "index-state-legacy"}])
def test_restore_rejects_timestamp_tie_without_shared_run_and_attempt(tmp_path, changes):
    from index_scrapers.artifacts import restore, StateUnavailable
    pre = state_artifact(11770091838, "2026-10-13T01:03:00Z", phase="pre")
    final = state_artifact(11769901956, pre["created_at"], **changes)
    def endpoint(request):
        assert request.url.path.endswith("/artifacts")
        return httpx.Response(200, json={"artifacts": [pre, final]})
    with httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        with pytest.raises(StateUnavailable, match="ambiguous"):
            restore(client, "owner/repo", "github-test-token", tmp_path / "restored.sqlite")
    assert not (tmp_path / "restored.sqlite").exists()


@pytest.mark.parametrize("changes", [{"run": 38298310420}, {"attempt": 2}, {"name": "index-state-legacy"}])
def test_prune_refuses_ambiguous_timestamp_at_retention_boundary(changes):
    pre = state_artifact(11770091838, "2026-10-13T01:03:00Z", phase="pre")
    final = state_artifact(11769901956, pre["created_at"], **changes)
    newer = state_artifact(11770091839, "2026-10-13T01:04:00Z", run=38298310421)
    newest = state_artifact(11770091840, "2026-10-13T01:05:00Z", run=38298310422)
    prune_snapshots([pre, final, newer, newest], error="ambiguous snapshot order")


def test_prune_only_counts_trusted_unexpired_production_snapshots():
    oldest = state_artifact(10, "2026-10-13T01:00:00Z")
    retained = [state_artifact(identifier, f"2026-10-13T01:0{identifier - 10}:00Z") for identifier in (11, 12, 13)]
    unrelated = {"id": 99, "name": "other-artifact"}
    expired = state_artifact(98, "2026-10-13T02:00:00Z", expired=True)
    untrusted = []
    for metadata in [{"head_branch": "feature"}, {"head_repository_id": 2}, {"repository_id": 2}, {"repository_id": None}]:
        artifact = state_artifact(97 - len(untrusted), "2026-10-13T02:00:00Z")
        artifact["workflow_run"].update(metadata)
        untrusted.append(artifact)
    assert prune_snapshots([oldest, *retained, unrelated, expired, *untrusted]) == [oldest["id"]]
