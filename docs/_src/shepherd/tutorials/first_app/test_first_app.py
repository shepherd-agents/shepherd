"""Executes the tutorial's documented example against the simulated offline
provider — what the docs show is what runs (DESIGN goal 2 / S5)."""

from tutorials.first_app.app import SAMPLE_DIFF, Triage, main, review_change, triage_change

import shepherd as sp


def test_triage_matches_documented_output():
    with sp.workspace(model="claude:sonnet-4-5"):
        triage = triage_change(SAMPLE_DIFF)
    assert isinstance(triage, Triage)
    assert (triage.category, triage.priority) == ("bugfix", "high")


def test_compose_two_tasks():
    with sp.workspace(model="claude:sonnet-4-5"):
        review = review_change(SAMPLE_DIFF)
    assert review.verdict == "approve"
    assert "auth.py" in review.summary


def test_main_runs_end_to_end():
    assert main().verdict == "approve"


def test_task_parameters_must_be_annotated():
    # Real 0.2.0 rule (the signature is the contract): every task parameter must
    # be annotated. A docstring is recommended but not required.
    import pytest

    with pytest.raises(TypeError, match="must be annotated"):
        @sp.task
        def unannotated(x) -> str:  # pragma: no cover - definition itself raises
            """Do a thing."""
