"""Native benchmark scoring, independent of checker inference."""

import argparse
import json
import math
from pathlib import Path


def prediction(result: dict, dataset: str) -> bool | None:
    """Preserve native identity-only versus metadata-sensitive scoring."""
    state = result["identity"]["state"]
    if state == "not_found" and result["investigation"]["sufficient"]:
        return True
    if state != "identified":
        return None
    if dataset == "delta_human_audit":
        return False
    outcomes = {c["outcome"] for c in result["field_checks"].values()}
    if "error" in outcomes:
        return True
    if outcomes == {"verified"}:
        return False
    return None


def metrics(cases: list[dict]) -> dict:
    """Count abstentions as incorrect and retain native class metrics."""
    n = len(cases)
    positive = sum(c["expected_positive"] for c in cases)
    negative = n - positive
    tp = sum(c["expected_positive"] and c["predicted_positive"] is True for c in cases)
    fp = sum(not c["expected_positive"] and c["predicted_positive"] is True for c in cases)
    fn = sum(c["expected_positive"] and c["predicted_positive"] is False for c in cases)
    tn = sum(not c["expected_positive"] and c["predicted_positive"] is False for c in cases)
    decided = tp + fp + fn + tn
    correct = tp + tn
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / positive if positive else None
    z = 1.959963984540054
    center = (correct / n + z * z / (2 * n)) / (1 + z * z / n) if n else 0
    radius = z * math.sqrt((correct / n) * (1 - correct / n) / n + z * z / (4 * n * n)) / (1 + z * z / n) if n else 0
    return {
        "n": n,
        "correct": correct,
        "decided": decided,
        "abstentions": n - decided,
        "accuracy": correct / n if n else None,
        "coverage": decided / n if n else None,
        "precision": precision,
        "recall": recall,
        "f1": 2 * tp / (2 * tp + fp + positive - tp) if 2 * tp + fp + positive - tp else None,
        "false_positive_rate": fp / negative if negative else None,
        "true_positives": tp,
        "false_positives": fp,
        "true_negatives": tn,
        "false_negatives_decided": fn,
        "positive_abstentions": positive - tp - fn,
        "negative_abstentions": negative - tn - fp,
        "accuracy_wilson_95": [max(0, center - radius), min(1, center + radius)] if n else None,
    }


def evaluate(report: dict, labels: dict) -> dict:
    """Require complete frozen population coverage, counting abstentions as errors."""
    rows = report["results"]
    if len(rows) != len(labels) or {row["id"] for row in rows} != set(labels):
        raise ValueError("Report must contain every labeled citation exactly once")
    allowed = {
        "manual_reference_verification": {"verified", "problematic"},
        "hallmark_test_public": {"VALID", "HALLUCINATED"},
        "delta_human_audit": {"EXISTS", "FABRICATED", "AMBIGUOUS"},
    }
    cases = []
    for row in rows:
        label = labels[row["id"]]
        if label["gold"] not in allowed[label["dataset"]]:
            raise ValueError("Unknown native gold label")
        expected = (
            None if label["gold"] == "AMBIGUOUS" else label["gold"] in {"problematic", "HALLUCINATED", "FABRICATED"}
        )
        predicted = prediction(row, label["dataset"])
        cases.append(
            {
                "id": row["id"],
                **label,
                "expected_positive": expected,
                "predicted_positive": predicted,
                "correct": None if expected is None else predicted is not None and predicted == expected,
            }
        )
    return {
        "comparisons": cases,
        "datasets": {
            name: metrics([c for c in cases if c["dataset"] == name and c["expected_positive"] is not None])
            for name in sorted({c["dataset"] for c in cases})
        },
    }


def main() -> None:
    """Score a saved report without network or model calls."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--labels", type=Path, default=Path(__file__).parent / "frozen/labels.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = evaluate(json.loads(args.report.read_text()), json.loads(args.labels.read_text()))
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    else:
        print(json.dumps(result["datasets"], indent=2))  # noqa: T201 -- CLI output


if __name__ == "__main__":
    main()
