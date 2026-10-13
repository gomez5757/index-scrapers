import json
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize(
    ("event_name", "event_inputs", "expected"),
    [
        ("workflow_dispatch", {"delivery_limit": "5"}, 5),
        ("workflow_dispatch", {"delivery_limit": "100"}, 100),
        ("workflow_dispatch", {}, 100),
        ("schedule", {}, 100),
    ],
)
def test_reusable_delivery_limit_is_a_number(event_name, event_inputs, expected):
    workflow = (Path(__file__).parents[1] / ".github/workflows/scrape.yml").read_text(
        encoding="utf-8"
    )
    collect = workflow.split("  collect:\n", 1)[1]
    value = next(
        line.split(":", 1)[1].strip()
        for line in collect.splitlines()
        if line.strip().startswith("delivery_limit:")
    )
    assert value.startswith("${{ ") and value.endswith(" }}")
    expression = value[3:-2].strip()
    node = shutil.which("node")
    assert node, "Node.js is required to evaluate the workflow input expression"
    context = {"event_name": event_name, "event": {"inputs": event_inputs}}
    # These operators share JavaScript semantics; fromJSON returns a JSON value.
    script = """
        const vm = require('node:vm');
        const github = JSON.parse(process.argv[2]);
        const value = vm.runInNewContext(process.argv[1], {
            github, inputs: github.event.inputs, fromJSON: JSON.parse
        });
        process.stdout.write(JSON.stringify(value));
    """
    result = subprocess.run(
        [node, "-e", script, expression, json.dumps(context)],
        check=True,
        capture_output=True,
        text=True,
    )
    actual = json.loads(result.stdout)
    assert type(actual) is int
    assert actual == expected
