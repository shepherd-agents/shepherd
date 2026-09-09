"""Retained scores and population checks survive removal of experimental runners."""

import importlib.util
import json
from pathlib import Path

import pytest

EVALUATION = Path(__file__).parents[1] / "evaluation"
SPEC = importlib.util.spec_from_file_location("citation_evaluation_score", EVALUATION / "score.py")
score = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(score)


def test_native_scores_reproduce_the_retained_full_run():
    report = json.loads((EVALUATION / "results/benchmark/report.json").read_text())
    labels = json.loads((EVALUATION / "frozen/labels.json").read_text())
    expected = json.loads((EVALUATION / "results/benchmark/evaluation.json").read_text())
    assert score.evaluate(report, labels) == expected
    # The incomplete citation stays in the denominator; ambiguous gold stays out.
    cases = score.evaluate(report, labels)["comparisons"]
    assert next(c for c in cases if c["id"] == "262")["correct"] is False
    assert sum(c["expected_positive"] is not None for c in cases) == 140


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown_gold"])
def test_population_or_label_changes_are_rejected(mutation):
    report = json.loads((EVALUATION / "results/benchmark/report.json").read_text())
    labels = json.loads((EVALUATION / "frozen/labels.json").read_text())
    if mutation == "missing":
        report["results"].pop()
    elif mutation == "duplicate":
        report["results"][-1] = report["results"][0]
    else:
        labels[report["results"][0]["id"]]["gold"] = "unrecognized"
    with pytest.raises(ValueError):
        score.evaluate(report, labels)
