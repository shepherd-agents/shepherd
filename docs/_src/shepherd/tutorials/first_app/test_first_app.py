"""Executes the tutorial's documented example against the simulated offline
provider — what the docs show is what runs (DESIGN goal 2 / S5)."""

from tutorials.first_app.app import SAMPLE_DIFF, Triage, main, review_change, triage_change

import shepherd as shp
from shepherd.providers import claude


def test_triage_matches_documented_output():
    with shp.workspace(model=claude("sonnet-4-5")):
        triage = triage_change(SAMPLE_DIFF)
    assert isinstance(triage, Triage)
    assert (triage.category, triage.priority) == ("bugfix", "high")


def test_compose_two_tasks():
    with shp.workspace(model=claude("sonnet-4-5")):
        review = review_change(SAMPLE_DIFF)
    assert review.verdict == "approve"
    assert "auth.py" in review.summary


def test_main_runs_end_to_end():
    assert main().verdict == "approve"


def test_bodyless_task_requires_docstring():
    import pytest

    with pytest.raises(TypeError, match="docstring or guidance"):
        @shp.task
        def nameless(x: str) -> str:  # pragma: no cover - definition itself raises
            pass
