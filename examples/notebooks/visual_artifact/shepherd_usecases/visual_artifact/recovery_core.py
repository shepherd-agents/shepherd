"""Pipeline recovery genre for the gradient-descent tile."""

# ruff: noqa: TC003

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .tile import ARTIFACT_PATH, TileBrief

PLAN_PATH = "plan.json"
DRAFT_PATH = ARTIFACT_PATH
DEFAULT_FRAMING = "contour-map"
AMENDMENT = "Reverse the update path so each step descends toward the minimum."


def make_plan(brief: TileBrief) -> dict[str, object]:
    return {
        "artifact": "gradient_descent_tile",
        "framing": DEFAULT_FRAMING,
        "request": brief.request,
        "must_include": list(brief.required_labels),
        "decision_critical": "update path must descend toward the minimum",
    }


def draft_instruction(plan: Mapping[str, object], amendment: str | None = None) -> str:
    base = f"Draft the tile for this request: {plan.get('request', '')}"
    return f"{base} Amendment: {amendment}" if amendment else base


@dataclass(frozen=True)
class RecoveryPlan:
    failure_type: str
    bad_step: str
    evidence: str
    retry_boundary: str
    recommended_change: str

    def to_lines(self) -> list[str]:
        return [
            f"failure_type:      {self.failure_type}",
            f"bad_step:          {self.bad_step}",
            f"evidence:          {self.evidence}",
            f"retry_boundary:    {self.retry_boundary}",
            f"recommended_change: {self.recommended_change}",
        ]


def classify_failure(issues: Sequence[str]) -> RecoveryPlan:
    evidence_detail = "; ".join(issues) or "review failed"
    text = " ".join(issues).lower()
    if "uphill" in text or "direction" in text or "minimum" in text:
        failure_type = "wrong_direction"
        recommended_change = AMENDMENT
    elif "label" in text or "structure" in text or "data-layout" in text:
        failure_type = "broken_tile_structure"
        recommended_change = "Restore the infographic tile structure and required labels."
    else:
        failure_type = "render_or_scope"
        recommended_change = "Fix the render/scope issue while preserving the planned tile."
    return RecoveryPlan(
        failure_type=failure_type,
        bad_step="draft",
        evidence=f"{ARTIFACT_PATH} -> {evidence_detail}",
        retry_boundary="after plan, before draft",
        recommended_change=recommended_change,
    )


__all__ = [
    "AMENDMENT",
    "DEFAULT_FRAMING",
    "DRAFT_PATH",
    "PLAN_PATH",
    "RecoveryPlan",
    "classify_failure",
    "draft_instruction",
    "make_plan",
]
