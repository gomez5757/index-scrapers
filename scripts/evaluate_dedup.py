"""Prepare real review pairs and evaluate human or explicit agent references.

Preparation reads existing JSONL scraper snapshots and a CSV chosen by a person
(`left_id,right_id`). It never downloads, invents, labels or predicts examples.
Fill each resulting JSONL record with label `duplicate` or `distinct`,
label_source `human`, reviewer, timezone-aware reviewed_at, sample_origin
`live_portal` and capture_reference (the real capture/manifest used for review).

    python scripts/evaluate_dedup.py prepare --listings captures.jsonl \
        --pairs selected_pairs.csv --output manual_review.jsonl
    python scripts/evaluate_dedup.py evaluate --pairs manual_review.jsonl \
        --output metrics.json
    python scripts/evaluate_dedup.py evaluate-agent --labels labels.jsonl \
        --reference review.jsonl --output agent_metrics.json

Include suspected duplicates and negative controls, including nearby homes,
missing photos/coordinates and tolerance boundaries. Human labels must be made
independently of algorithm predictions. Metadata records that attestation; this
tool cannot prove a person actually reviewed a URL. Sample rates do not estimate
population rates unless the sampling method supports that inference.

The explicit evaluate-agent mode accepts exactly 200 independently reviewed raw
labels (duplicate/distinct/doubtful) against 200 real unlabelled reference pairs.
Doubtful cases are excluded from rates and reported in coverage. These measures
compare rules with an AI reference; they are not human ground truth. Reviewer
isolation must be arranged outside this tool: provenance cannot prove blindness.
"""

import argparse
from collections.abc import Iterable, Mapping
from copy import deepcopy
import csv
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import sys
from urllib.parse import urlparse

from index_scrapers import dedup


MINIMUM_REVIEWED_PAIRS = 200
_DOMAINS = {"idealista": "idealista.com", "fotocasa": "fotocasa.es", "pisoscom": "pisos.com", "habitaclia": "habitaclia.com"}


def _pair_key(left, right):
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        raise ValueError("Each pair needs two listing snapshots")
    ids = (left.get("id"), right.get("id"))
    if any(not isinstance(value, str) or not value for value in ids) or ids[0] == ids[1]:
        raise ValueError("A pair needs two different stable listing ids")
    return tuple(sorted(ids))


def _pair_id(key):
    return sha256(json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _snapshot_source(item):
    portal = item.get("id", "").partition(":")[0]
    url = item.get("url")
    if not isinstance(url, str):
        raise ValueError("A listing snapshot needs its original portal URL")
    parsed = urlparse(url)
    domain = _DOMAINS.get(portal)
    hostname = parsed.hostname or ""
    if not domain or parsed.scheme != "https" or not (hostname == domain or hostname.endswith("." + domain)):
        raise ValueError("A listing snapshot needs its original portal URL")
    if not isinstance(item.get("property"), Mapping):
        raise ValueError("A listing snapshot needs its property data")


def prepare_pairs(listings: Iterable[Mapping], pair_ids: Iterable[tuple[str, str]]) -> list[dict]:
    """Collect only requested, existing snapshots with empty review metadata."""
    catalog = {}
    for item in listings:
        identity = item.get("id")
        if not isinstance(identity, str) or not identity:
            raise ValueError("Every listing snapshot needs a stable id")
        if identity in catalog and catalog[identity] != item:
            raise ValueError("Conflicting snapshots for one listing id")
        catalog[identity] = item
    prepared, seen = [], set()
    for left_id, right_id in pair_ids:
        if left_id not in catalog or right_id not in catalog:
            raise ValueError("Requested pair includes an unknown listing id")
        key = _pair_key(catalog[left_id], catalog[right_id])
        if key in seen:
            raise ValueError("Requested pair is repeated")
        seen.add(key)
        left, right = (catalog[identity] for identity in key)
        _snapshot_source(left)
        _snapshot_source(right)
        prepared.append({
            "pair_id": _pair_id(key), "left": deepcopy(left), "right": deepcopy(right),
            "label": None, "label_source": None, "reviewer": None, "reviewed_at": None,
            "sample_origin": None, "capture_reference": None, "notes": "",
        })
    return sorted(prepared, key=lambda pair: pair["pair_id"])


def calculate_metrics(decisions: Iterable[tuple[str, bool]]) -> dict:
    """Count a confusion matrix; this low-level helper never certifies a sample."""
    counts = {"true_positive": 0, "false_positive": 0, "false_negative": 0, "true_negative": 0}
    for label, prediction in decisions:
        if not isinstance(label, str) or label not in {"duplicate", "distinct"} or not isinstance(prediction, bool):
            raise ValueError("Metrics need valid labels and boolean predictions")
        if label == "duplicate":
            counts["true_positive" if prediction else "false_negative"] += 1
        else:
            counts["false_positive" if prediction else "true_negative"] += 1
    negatives = counts["false_positive"] + counts["true_negative"]
    positives = counts["false_negative"] + counts["true_positive"]
    return {
        "pair_count": sum(counts.values()), "counts": counts,
        "false_positive_rate": counts["false_positive"] / negatives if negatives else None,
        "false_negative_rate": counts["false_negative"] / positives if positives else None,
    }


def _validate_review(record):
    label = record.get("label")
    if not isinstance(label, str) or label not in {"duplicate", "distinct"}:
        raise ValueError("Every pair needs a resolved human label: duplicate or distinct")
    if record.get("label_source") != "human":
        raise ValueError("Labels must come from independent human review, never the algorithm")
    for field in ("reviewer", "capture_reference"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            raise ValueError(f"Every pair needs {field}")
    if record.get("sample_origin") != "live_portal":
        raise ValueError("Only real live_portal samples may be evaluated; fixtures are not evidence")
    try:
        reviewed_at = datetime.fromisoformat(record["reviewed_at"].replace("Z", "+00:00"))
        if reviewed_at.tzinfo is None:
            raise ValueError("Review timestamp needs a timezone")
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Every pair needs a valid timezone-aware reviewed_at") from exc


def evaluate_pairs(records: Iterable[Mapping]) -> dict:
    """Validate manual evidence before producing any accuracy measurements."""
    reviewed, seen, snapshots = [], set(), {}
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("Review records must be JSON objects")
        _validate_review(record)
        left, right = record.get("left"), record.get("right")
        key = _pair_key(left, right)
        if key in seen:
            raise ValueError("A reviewed pair is repeated, including in reverse order")
        seen.add(key)
        if record.get("pair_id") is not None and record["pair_id"] != _pair_id(key):
            raise ValueError("pair_id does not identify the supplied listings")
        for item in (left, right):
            _snapshot_source(item)
            if item["id"] in snapshots and snapshots[item["id"]] != item:
                raise ValueError("Conflicting listing snapshots inside the evaluation dataset")
            snapshots[item["id"]] = item
        reviewed.append((key, record))
    if len(reviewed) < MINIMUM_REVIEWED_PAIRS:
        raise ValueError("Evaluation requires at least 200 distinct pairs independently labelled by humans")
    if {record["label"] for _, record in reviewed} != {"duplicate", "distinct"}:
        raise ValueError("Both duplicate and distinct human labels are required to measure FP and FN")
    ordered = [record for _, record in sorted(reviewed, key=lambda pair: pair[0])]
    canonical = json.dumps(ordered, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    metrics = calculate_metrics((record["label"], dedup.is_duplicate(record["left"], record["right"])) for record in ordered)
    return metrics | {
        "algorithm_version": dedup.ALGORITHM_VERSION,
        "algorithm_sha256": sha256(Path(dedup.__file__).read_bytes()).hexdigest(),
        "dataset_sha256": sha256(canonical.encode()).hexdigest(),
        "scope": "Pair-rule metrics on the supplied manually reviewed sample; not population accuracy or catalogue-group accuracy",
    }


def _agent_reference(records):
    records = list(records)
    if len(records) != 200:
        raise ValueError("Agent reference requires exactly 200 raw pairs")
    pairs, seen_keys, snapshots = {}, set(), {}
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("Agent reference records must be JSON objects")
        if record.get("sample_origin") != "live_portal" or not isinstance(record.get("capture_reference"), str) or not record["capture_reference"].strip():
            raise ValueError("Agent reference needs real live_portal capture provenance")
        if any(record.get(field) is not None for field in ("label", "label_source", "reviewer", "reviewed_at")):
            raise ValueError("Agent reference must be unlabelled")
        left, right = record.get("left"), record.get("right")
        key = _pair_key(left, right)
        identity = record.get("pair_id")
        if key in seen_keys or isinstance(identity, str) and identity in pairs:
            raise ValueError("Agent reference pair is repeated")
        if not isinstance(identity, str) or identity != _pair_id(key):
            raise ValueError("Reference pair_id does not identify its listing snapshots")
        for item in (left, right):
            _snapshot_source(item)
            if item["id"] in snapshots and snapshots[item["id"]] != item:
                raise ValueError("Conflicting listing snapshots inside agent reference")
            snapshots[item["id"]] = item
        seen_keys.add(key)
        pairs[identity] = record
    return pairs


def _algorithm_fields(value):
    forbidden = {"photo_hash", "phash", "perceptual_hash", "prediction", "predictions", "predicted_duplicate",
                 "algorithm_prediction", "algorithm_label", "algorithm_version", "algorithm_sha256"}
    if isinstance(value, Mapping):
        return any(str(key).casefold() in forbidden or _algorithm_fields(child) for key, child in value.items())
    if isinstance(value, list):
        return any(_algorithm_fields(child) for child in value)
    return False


def _validate_agent_label(record):
    if not isinstance(record, Mapping):
        raise ValueError("Agent labels must be JSON objects")
    label = record.get("label")
    if not isinstance(label, str) or label not in {"duplicate", "distinct", "doubtful"}:
        raise ValueError("Agent label must be duplicate, distinct or doubtful")
    if record.get("label_source") != "independent_agent":
        raise ValueError("Agent labels require independent_agent provenance, never algorithm or human labels")
    if _algorithm_fields(record):
        raise ValueError("Agent labels contain algorithm predictions or photo hashes")
    for field in ("reviewer", "notes"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            raise ValueError(f"Agent labels need non-empty {field}")
    evidence = record.get("evidence_photo_refs")
    if not isinstance(evidence, list) or not evidence or any(not isinstance(ref, str) or not ref.strip() for ref in evidence):
        raise ValueError("Agent labels need non-empty evidence_photo_refs")
    try:
        reviewed_at = datetime.fromisoformat(record["reviewed_at"].replace("Z", "+00:00"))
        if reviewed_at.tzinfo is None:
            raise ValueError("Review timestamp needs a timezone")
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Agent labels need a valid timezone-aware reviewed_at") from exc


def evaluate_independent_agent(labels: Iterable[Mapping], reference: Iterable[Mapping]) -> dict:
    """Measure agreement with frozen agent labels without changing those labels."""
    labels = list(labels)
    if len(labels) != 200:
        raise ValueError("Agent evaluation requires exactly 200 raw labels, including doubtful")
    pairs = _agent_reference(reference)
    by_id = {}
    for record in labels:
        _validate_agent_label(record)
        identity = record.get("pair_id")
        if not isinstance(identity, str) or identity not in pairs:
            raise ValueError("Agent label pair_id is outside the frozen reference")
        if identity in by_id:
            raise ValueError("Agent label pair_id is repeated")
        for side in ("left", "right"):
            if f"{side}_id" in record and record[f"{side}_id"] != pairs[identity][side]["id"]:
                raise ValueError("Agent label listing id disagrees with the reference")
        by_id[identity] = record
    if set(by_id) != set(pairs):
        raise ValueError("Agent labels must cover exactly the frozen reference ids")
    decisions, false_positives, false_negatives, doubtful = [], [], [], []
    for identity in sorted(pairs):
        label = by_id[identity]["label"]
        if label == "doubtful":
            doubtful.append(identity)
            continue
        record = pairs[identity]
        prediction = dedup.is_duplicate(record["left"], record["right"])
        decisions.append((label, prediction))
        if label == "distinct" and prediction:
            false_positives.append(identity)
        elif label == "duplicate" and not prediction:
            false_negatives.append(identity)
    metrics = calculate_metrics(decisions)
    ordered_reference = [pairs[identity] for identity in sorted(pairs)]
    ordered_labels = [by_id[identity] for identity in sorted(by_id)]
    reference_json = json.dumps(ordered_reference, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    labels_json = json.dumps(ordered_labels, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return metrics | {
        "pair_count": len(pairs), "resolved_pair_count": len(decisions), "doubtful_pair_count": len(doubtful),
        "coverage": len(decisions) / len(pairs), "reference_kind": "independent_agent",
        "false_positive_pair_ids": false_positives, "false_negative_pair_ids": false_negatives, "doubtful_pair_ids": doubtful,
        "reference_sha256": sha256(reference_json.encode()).hexdigest(), "labels_sha256": sha256(labels_json.encode()).hexdigest(),
        "algorithm_version": dedup.ALGORITHM_VERSION, "algorithm_sha256": sha256(Path(dedup.__file__).read_bytes()).hexdigest(),
        "scope": "Pair-rule agreement with an independent-agent reference; doubtful excluded; not human ground truth, population or catalogue-group accuracy",
        "independence": "Declared reviewer provenance; reviewer isolation is established outside this tool",
    }


def _read_jsonl(path):
    with Path(path).open(encoding="utf-8-sig") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"JSONL line {line_number} must be an object")
            yield value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Copy existing snapshots into an unlabelled human review template")
    prepare.add_argument("--listings", required=True, help="JSONL of real saved scraper records")
    prepare.add_argument("--pairs", required=True, help="CSV with left_id,right_id, selected independently")
    prepare.add_argument("--output", required=True, help="New JSONL file; existing files are preserved")
    evaluate = commands.add_parser("evaluate", help="Measure rules against >=200 independently reviewed real pairs")
    evaluate.add_argument("--pairs", required=True)
    evaluate.add_argument("--output", help="New JSON metrics file; otherwise print to stdout")
    agent = commands.add_parser("evaluate-agent", help="Explicit AI-reference comparison of exactly 200 raw pairs")
    agent.add_argument("--labels", required=True, help="Frozen independent_agent labels including doubtful")
    agent.add_argument("--reference", required=True, help="The original 200 real unlabelled pair snapshots")
    agent.add_argument("--output", help="New JSON metrics file; otherwise print to stdout")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            with Path(args.pairs).open(encoding="utf-8-sig", newline="") as source:
                reader = csv.DictReader(source)
                if not reader.fieldnames or not {"left_id", "right_id"} <= set(reader.fieldnames):
                    raise ValueError("Pair CSV needs left_id,right_id columns")
                pair_ids = [(row["left_id"], row["right_id"]) for row in reader]
            prepared = prepare_pairs(_read_jsonl(args.listings), pair_ids)
            with Path(args.output).open("x", encoding="utf-8") as destination:
                for record in prepared:
                    destination.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
            print(f"Prepared {len(prepared)} unlabelled pairs; no accuracy metrics were computed.")
        else:
            if args.command == "evaluate-agent":
                metrics = evaluate_independent_agent(_read_jsonl(args.labels), _read_jsonl(args.reference))
            else:
                metrics = evaluate_pairs(_read_jsonl(args.pairs))
            rendered = json.dumps(metrics, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
            if args.output:
                with Path(args.output).open("x", encoding="utf-8") as destination:
                    destination.write(rendered)
            else:
                print(rendered, end="")
    except (OSError, ValueError, UnicodeError) as exc:
        print(f"Evaluation refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
