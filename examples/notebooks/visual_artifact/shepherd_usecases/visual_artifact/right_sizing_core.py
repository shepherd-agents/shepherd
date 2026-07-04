"""Model right-sizing genre over the gradient-descent tile gate."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .tile import ARTIFACT_PATH, TileBrief, evaluate_gate

VERDICTS_PATH = "verdicts.json"


@dataclass(frozen=True)
class EvalConfig:
    name: str
    model: str
    rel_cost: str
    rel_latency: str
    cost_estimate: float
    latency_estimate: float


CONFIGS: tuple[EvalConfig, ...] = (
    EvalConfig("high", "opus", "high", "slow", 0.090, 9.0),
    EvalConfig("mid", "sonnet", "medium", "medium", 0.030, 4.0),
    EvalConfig("cheap", "haiku", "low", "fast", 0.008, 1.5),
)
CONFIG_BY_NAME: dict[str, EvalConfig] = {c.name: c for c in CONFIGS}


def ground_truth_labels(candidates: Sequence[Mapping[str, str]], brief: TileBrief) -> dict[str, str]:
    labels: dict[str, str] = {}
    for candidate in candidates:
        report = evaluate_gate(
            branch=candidate["id"],
            html=candidate["html"],
            changed_paths=(ARTIFACT_PATH,),
            brief=brief,
        )
        labels[candidate["id"]] = "pass" if report.passed else "fail"
    return labels


def hard_failures(labels: Mapping[str, str]) -> set[str]:
    return {cid for cid, label in labels.items() if label == "fail"}


def evaluator_prompt(brief: Mapping[str, object], candidates: Sequence[Mapping[str, str]]) -> str:
    blocks = "\n\n".join(f'### candidate id="{c["id"]}"\n```html\n{c["html"]}\n```' for c in candidates)
    return f"""You are a QA reviewer for gradient-descent infographic tiles.

Brief:
{json.dumps(dict(brief), indent=2, sort_keys=True)}

A candidate FAILS if its update path moves uphill or away from the minimum. It
also fails if the tile is not self-contained HTML with inline SVG and the required
labels. Harmless visual styling differences are acceptable; the direction of the
optimization path is decision-critical.

Candidates:
{blocks}

Return JSON matching the schema: for every candidate id, give direction_check,
structure, verdict, and rationale; then name selected_candidate.
"""


def evaluator_output_schema() -> dict[str, object]:
    verdict = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "direction_check": {"enum": ["pass", "fail"]},
            "structure": {"enum": ["pass", "fail"]},
            "verdict": {"enum": ["pass", "fail"]},
            "rationale": {"type": "string"},
        },
        "required": ["id", "direction_check", "structure", "verdict", "rationale"],
    }
    return {
        "type": "object",
        "properties": {
            "candidates": {"type": "array", "items": verdict},
            "selected_candidate": {"type": "string"},
        },
        "required": ["candidates", "selected_candidate"],
    }


def static_evaluator_verdicts(
    candidates: Sequence[Mapping[str, str]],
    config: EvalConfig,
    labels: Mapping[str, str],
    *,
    plant_cheap_miss: bool = True,
) -> dict[str, object]:
    misses = plant_cheap_miss and config.name == "cheap"
    verdicts = []
    for candidate in candidates:
        cid = candidate["id"]
        truth_fail = labels.get(cid) == "fail"
        sees_fail = truth_fail and not misses
        verdicts.append(
            {
                "id": cid,
                "direction_check": "fail" if sees_fail else "pass",
                "structure": "pass",
                "verdict": "fail" if sees_fail else "pass",
                "rationale": (
                    "The path climbs away from the minimum."
                    if sees_fail
                    else "The tile appears self-contained and directionally correct."
                ),
            }
        )
    passing = [v["id"] for v in verdicts if v["verdict"] == "pass" and labels.get(v["id"]) != "fail"]
    selected = passing[0] if passing else (verdicts[0]["id"] if verdicts else "")
    return {"candidates": verdicts, "selected_candidate": selected}


@dataclass(frozen=True)
class ConfigReport:
    config: str
    model: str
    schema: str
    hard_fail_catch: str
    cost: str
    latency: str
    cost_value: float
    latency_value: float
    passed: bool
    failures: tuple[str, ...] = ()

    def to_row(self) -> dict[str, object]:
        return {
            "config": self.config,
            "model": self.model,
            "cost": self.cost,
            "latency": self.latency,
            "schema": self.schema,
            "catches_hard_fail": self.hard_fail_catch,
            "passed": self.passed,
        }


def verdicts_by_id(verdicts: Mapping[str, object]) -> dict[str, Mapping[str, object]] | None:
    rows = verdicts.get("candidates") if isinstance(verdicts, Mapping) else None
    well_formed = isinstance(rows, list) and all(
        isinstance(row, Mapping) and {"id", "verdict", "direction_check"} <= set(row) for row in rows
    )
    return {str(row["id"]): row for row in rows} if well_formed else None


def caught_hard_failures(verdicts: Mapping[str, object], labels: Mapping[str, str]) -> tuple[int, int]:
    by_id = verdicts_by_id(verdicts) or {}
    expected = hard_failures(labels)
    caught = sum(
        1
        for cid in expected
        if by_id.get(cid, {}).get("verdict") == "fail" or by_id.get(cid, {}).get("direction_check") == "fail"
    )
    return caught, len(expected)


def gate_config(
    config: EvalConfig,
    verdicts: Mapping[str, object],
    labels: Mapping[str, str],
    *,
    cost_value: float,
    latency_value: float,
) -> ConfigReport:
    schema_ok = verdicts_by_id(verdicts) is not None
    caught, expected = caught_hard_failures(verdicts, labels)
    failures: list[str] = []
    if not schema_ok:
        failures.append("evaluator output failed schema")
    elif caught < expected:
        failures.append(f"missed {expected - caught} decision-critical divergence(s)")
    return ConfigReport(
        config=config.name,
        model=config.model,
        schema="pass" if schema_ok else "fail",
        hard_fail_catch=f"{caught}/{expected}",
        cost=config.rel_cost,
        latency=config.rel_latency,
        cost_value=cost_value,
        latency_value=latency_value,
        passed=schema_ok and caught == expected,
        failures=tuple(failures),
    )


def cheapest_passing(reports: Sequence[ConfigReport]) -> ConfigReport | None:
    passing = [report for report in reports if report.passed]
    if not passing:
        return None
    return min(passing, key=lambda report: report.cost_value)
