"""Exercise workflow gates and scripts without dispatching GitHub Actions."""

import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import textwrap
from zipfile import ZipFile

import httpx
import pytest

from index_scrapers import artifacts


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/import-observations.yml"


def workflow_steps():
    source = WORKFLOW.read_text(encoding="utf-8")
    blocks = re.split(r"(?m)^      - id: ", source)[1:]
    steps = {}
    for block in blocks:
        identifier, body = block.split("\n", 1)
        fields = dict(re.findall(r"(?m)^        ([\w-]+): (.*)$", body))
        run = fields.get("run")
        if run == "|":
            match = re.search(r"(?m)^        run: \|\n((?:          .*\n|\n)+)", body)
            assert match, f"missing script for {identifier}"
            run = textwrap.dedent(match[1])
        steps[identifier] = {"body": body, "condition": fields.get("if"), "run": run}
    assert steps, "workflow has no named steps"
    return steps


def simulate_gates(*, apply=False, deliver=False, failures=()):
    """Evaluate the actual Actions expressions with the runner's status semantics."""
    node = shutil.which("node")
    assert node, "Node.js is required to evaluate workflow expressions"
    script = r"""
        const vm = require('node:vm');
        const [definitions, apply, deliver, failures] = JSON.parse(process.argv[1]);
        const steps = Object.fromEntries(Object.keys(definitions).map(id =>
            [id, {outcome: 'skipped', outputs: {}}]));
        const executed = [];
        let failed = false;
        for (const [id, definition] of Object.entries(definitions)) {
            const context = {steps, always: () => true, success: () => !failed};
            let enabled = !failed;
            if (definition.condition) {
                const expression = definition.condition.slice(3, -2).trim();
                enabled = Boolean(vm.runInNewContext(expression, context));
                // GitHub applies success() implicitly without a status function.
                if (!/\b(always|success|failure|cancelled)\s*\(/.test(expression)) {
                    enabled = enabled && !failed;
                }
            }
            if (!enabled) continue;
            executed.push(id);
            steps[id].outcome = failures.includes(id) ? 'failure' : 'success';
            if (id === 'validate' && steps[id].outcome === 'success') {
                steps[id].outputs = {apply: String(apply), deliver: String(deliver)};
            }
            if (id === 'apply') steps[id].outputs.attempted = 'true';
            failed ||= steps[id].outcome === 'failure';
        }
        process.stdout.write(JSON.stringify({executed, steps, failed}));
    """
    definitions = {identifier: {"condition": step["condition"]} for identifier, step in workflow_steps().items()}
    result = subprocess.run(
        [node, "-e", script, json.dumps([definitions, apply, deliver, list(failures)])],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def test_manual_inputs_and_state_concurrency_are_safe_defaults():
    source = WORKFLOW.read_text(encoding="utf-8")
    assert "  workflow_dispatch:\n" in source
    assert "  schedule:" not in source
    assert "workflow_run:" not in source
    assert "initialize_state:" not in source and "--initialize" not in source
    for name in ("apply", "deliver"):
        block = re.split(r"(?m)^      \w+:", source.split(f"      {name}:\n", 1)[1], maxsplit=1)[0]
        assert "        type: boolean\n" in block
        assert "        default: false\n" in block
    assert "        type: number\n        default: 5\n" in source
    assert "        required: true\n        type: number\n" in source
    concurrency = source.split("concurrency:\n", 1)[1].split("permissions:\n", 1)[0]
    for other in ("scrape.yml", "health.yml"):
        existing = (WORKFLOW.parent / other).read_text(encoding="utf-8")
        assert existing.split("concurrency:\n", 1)[1].split("permissions:\n", 1)[0] == concurrency
    assert "  group: index-durable-state\n  cancel-in-progress: false\n  queue: max\n" in concurrency
    assert "permissions:\n  contents: read\n  actions: read\n" in source
    assert "github.ref_name == github.event.repository.default_branch" in source
    assert "deleteArtifact" not in source


@pytest.mark.parametrize(
    ("apply", "deliver", "artifact", "limit", "valid"),
    [
        ("false", "false", "123", "5", True),
        ("true", "false", "123", "100", True),
        ("true", "true", "9007199254740991", "1", True),
        ("false", "true", "123", "5", False),
        ("True", "false", "123", "5", False),
        ("true", "1", "123", "5", False),
        ("false", "false", "0", "5", False),
        ("false", "false", "9007199254740992", "5", False),
        ("false", "false", "1e3", "5", False),
        ("false", "false", "123", "0", False),
        ("false", "false", "123", "101", False),
        ("false", "false", "123", "5.0", False),
        ("false", "false", "$(touch injected)", "5", False),
        ("false", "false", "123\napply=true", "5", False),
    ],
)
def test_validation_uses_environment_and_rejects_unsafe_options(tmp_path, apply, deliver, artifact, limit, valid):
    script = workflow_steps()["validate"]["run"]
    assert script.startswith("python - <<'PY'\n") and script.endswith("PY\n")
    python = script[len("python - <<'PY'\n"):-len("PY\n")]
    output = tmp_path / "outputs"
    environment = dict(os.environ, APPLY=apply, DELIVER=deliver, SOURCE_ARTIFACT_ID=artifact,
                       DELIVERY_LIMIT=limit, GITHUB_OUTPUT=str(output))
    result = subprocess.run([sys.executable, "-c", python], env=environment, cwd=tmp_path,
                            capture_output=True, text=True)
    assert (result.returncode == 0) is valid
    if valid:
        assert output.read_text(encoding="utf-8") == (
            f"artifact_id={artifact}\napply={apply}\ndeliver={deliver}\ndelivery_limit={limit}\n"
        )
    else:
        assert not output.exists()
    assert not (tmp_path / "injected").exists()


def test_shell_scripts_have_no_input_interpolation_or_ingest_secrets():
    steps = workflow_steps()
    for identifier, step in steps.items():
        if step["run"]:
            assert "${{" not in step["run"], identifier
        if "INGEST_URL:" in step["body"] or "INGEST_TOKEN:" in step["body"]:
            assert identifier in {"maintenance", "deliver"}
    source = WORKFLOW.read_text(encoding="utf-8")
    for name, expression in (("SOURCE_ARTIFACT_ID", "source_artifact_id"), ("APPLY", "apply"),
                             ("DELIVER", "deliver"), ("DELIVERY_LIMIT", "delivery_limit")):
        assert f"      {name}: ${{{{ inputs.{expression} }}}}\n" in source
    assert '--artifact-id "$SOURCE_ARTIFACT_ID"' in steps["fetch"]["run"]
    assert '--repository "$GITHUB_REPOSITORY"' in steps["fetch"]["run"]
    assert '--limit "$DELIVERY_LIMIT"' in steps["deliver"]["run"]
    assert '--state "$STATE_PATH"' in steps["restore"]["run"]
    assert "--apply" not in steps["preview"]["run"]
    assert steps["apply"]["run"].splitlines()[0] == 'echo \'attempted=true\' >> "$GITHUB_OUTPUT"'
    assert steps["apply"]["run"].splitlines()[1].endswith('"$APPLY_REPORT_PATH" --apply')
    assert "python scripts/maintain_fts.py --health" in steps["maintenance"]["run"]
    assert "Cloudflare" not in source and "wrangler" not in source


def test_dry_run_only_restores_fetches_previews_and_retains_inputs():
    result = simulate_gates()
    assert not result["failed"]
    assert result["executed"] == ["checkout", "setup", "install", "validate", "restore", "fetch", "preview", "retain_inputs"]
    steps = workflow_steps()
    retained = steps["retain_inputs"]["body"]
    assert "observation-package/" in retained
    assert "observation-preview-report.json" in retained
    assert "index-state-" not in retained
    assert "if-no-files-found: error" in retained
    assert "compression-level: 6" in retained


def test_apply_retains_originals_and_reports_before_state_and_never_delivers_by_default():
    executed = simulate_gates(apply=True)["executed"]
    assert executed.index("preview") < executed.index("retain_inputs") < executed.index("apply")
    assert executed.index("apply") < executed.index("retain_report") < executed.index("snapshot_prepare")
    assert executed.index("snapshot_prepare") < executed.index("upload_prepare") < executed.index("snapshot_final") < executed.index("upload_final")
    assert "maintenance" not in executed and "deliver" not in executed
    steps = workflow_steps()
    for identifier in ("retain_report", "retain_delivery_report"):
        assert "compression-level: 6" in steps[identifier]["body"]
    for identifier in ("upload_prepare", "upload_final"):
        assert "compression-level: 0" in steps[identifier]["body"]


@pytest.mark.parametrize("failure", ["validate", "restore", "fetch", "preview", "retain_inputs"])
def test_pre_application_failures_cannot_mutate_or_publish_state(failure):
    result = simulate_gates(apply=True, deliver=True, failures=[failure])
    assert result["failed"]
    assert not {"apply", "snapshot_prepare", "upload_prepare", "maintenance", "deliver", "snapshot_final", "upload_final"}.intersection(result["executed"])


@pytest.mark.parametrize("failure", ["apply", "retain_report", "snapshot_prepare", "upload_prepare", "maintenance"])
def test_failures_after_apply_block_delivery_and_keep_final_state(failure):
    result = simulate_gates(apply=True, deliver=True, failures=[failure])
    assert result["failed"]
    assert "deliver" not in result["executed"]
    assert "snapshot_final" in result["executed"] and "upload_final" in result["executed"]
    if failure in {"apply", "retain_report", "snapshot_prepare", "upload_prepare"}:
        assert "maintenance" not in result["executed"]
    if failure == "apply":
        assert "retain_report" in result["executed"]
        assert "snapshot_prepare" in result["executed"] and "upload_prepare" in result["executed"]


def test_explicit_delivery_waits_for_pre_snapshot_and_preserves_final_after_delivery_failure():
    result = simulate_gates(apply=True, deliver=True, failures=["deliver"])
    executed = result["executed"]
    assert executed.index("apply") < executed.index("upload_prepare") < executed.index("maintenance") < executed.index("deliver")
    assert executed.index("deliver") < executed.index("snapshot_final") < executed.index("upload_final")
    assert "retain_delivery_report" in executed and result["failed"]


def test_failed_final_snapshot_is_not_uploaded_as_an_empty_success():
    result = simulate_gates(apply=True, failures=["snapshot_final"])
    assert "upload_prepare" in result["executed"]
    assert "upload_final" not in result["executed"]
    assert result["failed"]


def test_restore_validates_latest_snapshot_without_initializing_state(tmp_path, monkeypatch):
    original = tmp_path / "original.sqlite"
    with sqlite3.connect(original) as database:
        for name in ("catalog", "queue", "progress"):
            database.execute(f"CREATE TABLE {name} (value TEXT)")
        database.execute("INSERT INTO catalog VALUES ('retained')")
    content = original.read_bytes()
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"schema": 1, "sha256": hashlib.sha256(content).hexdigest()}))
        archive.writestr("catalog.sqlite.gz", gzip.compress(content))

    def snapshot(identifier, created, *, fork=False):
        return {"id": identifier, "name": f"index-state-{identifier}-1-final", "created_at": created,
                "expired": False, "workflow_run": {"head_branch": "main", "repository_id": 1415957925,
                                                      "head_repository_id": 1 if fork else 1415957925}}

    available = [snapshot(800, "2026-10-14T10:00:00Z"), snapshot(100, "2026-10-14T11:00:00Z"),
                 snapshot(900, "2026-10-14T12:00:00Z", fork=True)]
    requested = []

    def transport(request):
        requested.append(request.url.path)
        if request.url.path.endswith("/artifacts"):
            return httpx.Response(200, json={"artifacts": available})
        assert request.url.path.endswith("/artifacts/100/zip")
        return httpx.Response(200, content=buffer.getvalue())

    def forbidden_state(*args, **kwargs):
        pytest.fail("restore without initialization must not open State")

    monkeypatch.setattr(artifacts, "State", forbidden_state)
    destination = tmp_path / "restored/catalog.sqlite"
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        result = artifacts.restore(client, "owner/repository", "fixture-token", destination,
                                   initialize=False, default_branch="main")
        assert result == "100" and destination.read_bytes() == content
        with pytest.raises(artifacts.StateUnavailable, match="overwrite local state"):
            artifacts.restore(client, "owner/repository", "fixture-token", destination,
                              initialize=False, default_branch="main")
    assert len(requested) == 2
    assert not destination.with_suffix(".restore.tmp").exists()
