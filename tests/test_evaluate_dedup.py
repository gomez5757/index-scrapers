"""Small evaluator fixtures exercise safeguards, never claim real accuracy."""

from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_dedup.py"


def evaluator():
    spec = importlib.util.spec_from_file_location("evaluate_dedup", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def snapshots():
    return [
        {"id": "idealista:1", "url": "https://www.idealista.com/inmueble/1/", "price": 1000,
         "photo_hash": "aaaaaaaaaaaaaaaa", "property": {"municipality": "28079", "operation": "rent"}},
        {"id": "fotocasa:2", "url": "https://www.fotocasa.es/es/alquiler/vivienda/madrid/2/d", "price": 1050,
         "photo_hash": "aaaaaaaaaaaaaaaa", "property": {"municipality": "28079", "operation": "rent"}},
    ]


def reviewed_pair():
    left, right = snapshots()
    return {
        "left": left, "right": right, "label": "distinct", "label_source": "human",
        "reviewer": "unit-test-reviewer", "reviewed_at": "2026-10-12T14:00:00Z",
        "sample_origin": "live_portal", "capture_reference": "unit-test-only-not-a-real-capture",
    }


def test_prepare_pairs_copies_actual_snapshots_and_leaves_labels_blank():
    original = snapshots()
    pairs = evaluator().prepare_pairs(original, [("idealista:1", "fotocasa:2")])
    assert len(pairs) == 1
    assert {pairs[0]["left"]["id"], pairs[0]["right"]["id"]} == {"idealista:1", "fotocasa:2"}
    for field in ("label", "label_source", "reviewer", "reviewed_at", "sample_origin", "capture_reference"):
        assert pairs[0][field] is None
    pairs[0]["left"]["price"] = 999999
    assert original[0]["price"] == 1000
    assert original[1]["price"] == 1050


def test_prepare_pairs_is_reproducible_when_inputs_are_reordered():
    forward = evaluator().prepare_pairs(snapshots(), [("idealista:1", "fotocasa:2")])
    reverse = evaluator().prepare_pairs(list(reversed(snapshots())), [("fotocasa:2", "idealista:1")])
    assert forward == reverse


@pytest.mark.parametrize("pair_ids", [
    [("idealista:1", "unknown:5")],
    [("idealista:1", "idealista:1")],
    [("idealista:1", "fotocasa:2"), ("fotocasa:2", "idealista:1")],
])
def test_prepare_rejects_unknown_self_or_repeated_pairs(pair_ids):
    with pytest.raises(ValueError):
        evaluator().prepare_pairs(snapshots(), pair_ids)


def test_prepare_rejects_conflicting_listing_snapshots():
    changed = deepcopy(snapshots()[0])
    changed["price"] = 9999
    with pytest.raises(ValueError, match="listing"):
        evaluator().prepare_pairs([*snapshots(), changed], [("idealista:1", "fotocasa:2")])


def test_evaluation_requires_at_least_200_independently_reviewed_pairs():
    with pytest.raises(ValueError, match="200"):
        evaluator().evaluate_pairs([reviewed_pair()])


@pytest.mark.parametrize("source", ["algorithm", "inferred", "prediction", "independent_agent", None])
def test_evaluation_rejects_labels_derived_from_algorithm(source):
    pair = reviewed_pair()
    pair["label_source"] = source
    with pytest.raises(ValueError, match="human"):
        evaluator().evaluate_pairs([pair])


@pytest.mark.parametrize("changes", [
    {"reviewer": ""}, {"reviewed_at": "invalid"}, {"reviewed_at": "2026-10-12T14:00:00"},
    {"sample_origin": "fixture"}, {"capture_reference": ""}, {"label": None}, {"label": "uncertain"},
    {"label": []},
])
def test_evaluation_rejects_missing_provenance_or_unresolved_labels(changes):
    pair = reviewed_pair() | changes
    with pytest.raises(ValueError):
        evaluator().evaluate_pairs([pair])


def test_evaluation_rejects_reversed_duplicates_that_would_inflate_sample_size():
    pair = reviewed_pair()
    reverse = deepcopy(pair)
    reverse["left"], reverse["right"] = reverse["right"], reverse["left"]
    with pytest.raises(ValueError, match="repeated"):
        evaluator().evaluate_pairs([pair, reverse])


def test_confusion_counts_and_rate_denominators_are_hand_checked():
    result = evaluator().calculate_metrics([
        ("duplicate", True), ("distinct", True),
        ("duplicate", False), ("distinct", False), ("distinct", False),
    ])
    assert result["counts"] == {"true_positive": 1, "false_positive": 1, "false_negative": 1, "true_negative": 2}
    assert result["false_positive_rate"] == pytest.approx(1 / 3)
    assert result["false_negative_rate"] == 0.5
    assert result["pair_count"] == 5


def test_absent_ground_truth_class_does_not_manufacture_a_zero_rate():
    result = evaluator().calculate_metrics([("distinct", False)])
    assert result["false_positive_rate"] == 0
    assert result["false_negative_rate"] is None


@pytest.mark.parametrize("decisions", [[("unknown", True)], [("duplicate", "true")], [([], True)]])
def test_metrics_reject_invalid_labels_and_non_boolean_predictions(decisions):
    with pytest.raises(ValueError):
        evaluator().calculate_metrics(decisions)


def test_cli_prepares_blank_review_records_from_saved_listings(tmp_path):
    source = tmp_path / "listings.jsonl"
    source.write_text("\n".join(json.dumps(item) for item in snapshots()), encoding="utf-8")
    pairs = tmp_path / "pairs.csv"
    pairs.write_text("left_id,right_id\nidealista:1,fotocasa:2\n", encoding="utf-8")
    output = tmp_path / "review.jsonl"
    result = subprocess.run([sys.executable, str(SCRIPT), "prepare", "--listings", str(source),
                             "--pairs", str(pairs), "--output", str(output)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["label"] is None
    assert "prediction" not in record


def test_cli_refuses_unlabelled_data_without_writing_metrics(tmp_path):
    source = tmp_path / "review.jsonl"
    source.write_text(json.dumps(reviewed_pair() | {"label": None}), encoding="utf-8")
    output = tmp_path / "metrics.json"
    result = subprocess.run([sys.executable, str(SCRIPT), "evaluate", "--pairs", str(source),
                             "--output", str(output)], capture_output=True, text=True)
    assert result.returncode == 2
    assert not output.exists()
    assert "label" in result.stderr.lower()


def agent_test_inputs():
    """Invented unit inputs only, never exported as a real review/benchmark."""
    references, labels = [], []
    for index in range(200):
        left, right = snapshots()
        left["id"] = f"idealista:{1000 + index}"
        left["url"] = f"https://www.idealista.com/inmueble/{1000 + index}/"
        right["id"] = f"fotocasa:{2000 + index}"
        right["url"] = f"https://www.fotocasa.es/es/alquiler/vivienda/madrid/{2000 + index}/d"
        for item in (left, right):
            item["property"].update(kind="flat", district="Centro", lat=40.43, lng=-3.7, area_m2=100, rooms=2)
        if index in (2, 3):
            right["property"]["rooms"] = 3
        ids = sorted((left["id"], right["id"]))
        pair_id = sha256(json.dumps(ids, separators=(",", ":")).encode()).hexdigest()
        references.append({"pair_id": pair_id, "left": left, "right": right,
                           "sample_origin": "live_portal", "capture_reference": "unit-test-only-not-a-real-capture",
                           "label": None, "label_source": None, "reviewer": None, "reviewed_at": None})
        labels.append({"pair_id": pair_id, "label": "doubtful", "label_source": "independent_agent",
                       "reviewer": "unit-test-agent-not-a-real-reviewer", "reviewed_at": "2026-10-12T14:00:00Z",
                       "notes": "Synthetic unit case only", "evidence_photo_refs": ["P01", "P02"]})
    return references, labels


def test_agent_mode_excludes_doubtful_pairs_from_rates_and_reports_coverage():
    references, labels = agent_test_inputs()
    for row, label in zip(labels[:4], ("duplicate", "distinct", "duplicate", "distinct")):
        row["label"] = label
    before = deepcopy((references, labels))
    result = evaluator().evaluate_independent_agent(labels, references)
    assert result["pair_count"] == 200
    assert result["resolved_pair_count"] == 4
    assert result["doubtful_pair_count"] == 196
    assert result["coverage"] == 0.02
    assert result["counts"] == {"true_positive": 1, "false_positive": 1, "false_negative": 1, "true_negative": 1}
    assert result["false_positive_rate"] == 0.5
    assert result["false_negative_rate"] == 0.5
    assert result["false_positive_pair_ids"] == [labels[1]["pair_id"]]
    assert result["false_negative_pair_ids"] == [labels[2]["pair_id"]]
    assert result["reference_kind"] == "independent_agent"
    assert (references, labels) == before


def test_agent_mode_accepts_fewer_than_200_resolved_and_a_missing_class():
    references, labels = agent_test_inputs()
    labels[0]["label"] = "duplicate"
    result = evaluator().evaluate_independent_agent(labels, references)
    assert result["resolved_pair_count"] == 1
    assert result["doubtful_pair_count"] == 199
    assert result["coverage"] == 0.005
    assert result["false_positive_rate"] is None
    assert result["false_negative_rate"] == 0


def test_agent_mode_accepts_all_doubtful_without_manufacturing_rates():
    references, labels = agent_test_inputs()
    result = evaluator().evaluate_independent_agent(labels, references)
    assert result["resolved_pair_count"] == 0
    assert result["coverage"] == 0
    assert result["false_positive_rate"] is None
    assert result["false_negative_rate"] is None
    assert result["counts"] == {"true_positive": 0, "false_positive": 0, "false_negative": 0, "true_negative": 0}


@pytest.mark.parametrize("which,extra", [("labels", False), ("labels", True), ("reference", False)])
def test_agent_mode_requires_exactly_200_raw_labels_and_reference_pairs(which, extra):
    references, labels = agent_test_inputs()
    rows = labels if which == "labels" else references
    rows.append(deepcopy(rows[0])) if extra else rows.pop()
    with pytest.raises(ValueError, match="200"):
        evaluator().evaluate_independent_agent(labels, references)


def test_agent_mode_rejects_duplicate_label_ids():
    references, labels = agent_test_inputs()
    labels[1]["pair_id"] = labels[0]["pair_id"]
    with pytest.raises(ValueError, match="repeated"):
        evaluator().evaluate_independent_agent(labels, references)


def test_agent_mode_rejects_labels_outside_the_frozen_reference():
    references, labels = agent_test_inputs()
    labels[0]["pair_id"] = "unrelated-pair"
    with pytest.raises(ValueError, match="reference"):
        evaluator().evaluate_independent_agent(labels, references)


def test_agent_mode_rejects_repeated_reference_pairs():
    references, labels = agent_test_inputs()
    references[1] = deepcopy(references[0])
    with pytest.raises(ValueError, match="repeated"):
        evaluator().evaluate_independent_agent(labels, references)


def test_agent_mode_rejects_snapshot_ids_that_disagree_with_pair_id():
    references, labels = agent_test_inputs()
    references[0]["left"]["id"] = "idealista:changed"
    with pytest.raises(ValueError, match="pair_id"):
        evaluator().evaluate_independent_agent(labels, references)


def test_agent_mode_rejects_an_optional_listing_id_mismatch():
    references, labels = agent_test_inputs()
    labels[0]["left_id"] = "idealista:wrong"
    with pytest.raises(ValueError, match="listing"):
        evaluator().evaluate_independent_agent(labels, references)


@pytest.mark.parametrize("changes", [
    {"label_source": "algorithm"}, {"label_source": "human"}, {"label": "inferred"}, {"label": True},
    {"reviewer": ""}, {"reviewed_at": "invalid"}, {"reviewed_at": "2026-10-12T14:00:00"},
    {"notes": ""}, {"evidence_photo_refs": []}, {"evidence_photo_refs": [""]},
    {"photo_hash": "aaaaaaaaaaaaaaaa"}, {"prediction": True}, {"evidence": {"predicted_duplicate": True}},
])
def test_agent_mode_rejects_incomplete_provenance_or_algorithm_leakage(changes):
    references, labels = agent_test_inputs()
    labels[0].update(changes)
    with pytest.raises(ValueError):
        evaluator().evaluate_independent_agent(labels, references)


@pytest.mark.parametrize("changes", [{"sample_origin": "fixture"}, {"capture_reference": ""}, {"label": "duplicate"}])
def test_agent_mode_requires_unlabelled_real_reference_with_capture_provenance(changes):
    references, labels = agent_test_inputs()
    references[0].update(changes)
    with pytest.raises(ValueError):
        evaluator().evaluate_independent_agent(labels, references)


def test_agent_mode_results_and_fingerprints_do_not_depend_on_input_order():
    references, labels = agent_test_inputs()
    labels[0]["label"] = "duplicate"
    first = evaluator().evaluate_independent_agent(labels, references)
    second = evaluator().evaluate_independent_agent(list(reversed(labels)), list(reversed(references)))
    assert first == second
    changed = deepcopy(labels)
    changed[0]["label"] = "distinct"
    third = evaluator().evaluate_independent_agent(changed, references)
    assert first["labels_sha256"] != third["labels_sha256"]
    assert first["reference_sha256"] == third["reference_sha256"]


def test_cli_agent_mode_is_explicit_and_outputs_agent_reference_metrics(tmp_path):
    references, labels = agent_test_inputs()
    labels[0]["label"] = "duplicate"
    reference = tmp_path / "reference.jsonl"
    source = tmp_path / "labels.jsonl"
    output = tmp_path / "metrics.json"
    reference.write_text("\n".join(json.dumps(row) for row in references), encoding="utf-8")
    source.write_text("\n".join(json.dumps(row) for row in labels), encoding="utf-8")
    result = subprocess.run([sys.executable, str(SCRIPT), "evaluate-agent", "--labels", str(source),
                             "--reference", str(reference), "--output", str(output)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    metrics = json.loads(output.read_text(encoding="utf-8"))
    assert metrics["reference_kind"] == "independent_agent"
    assert metrics["pair_count"] == 200
    assert metrics["resolved_pair_count"] == 1


def test_cli_agent_mode_refuses_bad_ids_before_writing_metrics(tmp_path):
    references, labels = agent_test_inputs()
    labels[0]["pair_id"] = "wrong"
    reference = tmp_path / "reference.jsonl"
    source = tmp_path / "labels.jsonl"
    output = tmp_path / "metrics.json"
    reference.write_text("\n".join(json.dumps(row) for row in references), encoding="utf-8")
    source.write_text("\n".join(json.dumps(row) for row in labels), encoding="utf-8")
    result = subprocess.run([sys.executable, str(SCRIPT), "evaluate-agent", "--labels", str(source),
                             "--reference", str(reference), "--output", str(output)], capture_output=True, text=True)
    assert result.returncode == 2
    assert not output.exists()
    assert "reference" in result.stderr
