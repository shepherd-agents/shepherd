"""Dialect acceptance for the public `apply` settlement verb (T1 task #10, W2.4 matrix).

Crosses the real SPI: `ShepherdWorkspace.apply` / `RunOutput.apply` → the vcs-core
application coordinator, with the D7 authority lane hydrated from the recorded run
authority context (route ``retained_output_application``). Uses the examples harness
for genuinely parallel candidates forked from one basis (the W4 capstone shape).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_EXAMPLES = Path(__file__).resolve().parents[4] / "examples" / "workspace-handles"
if str(_EXAMPLES) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES))

from _support import copy_git_repo, demo_workspace, seed_selected_workspace

import shepherd_dialect.workspace_control.workspace as workspace_module
from shepherd_dialect.workspace_control import WorkspaceControlError

_PATH_TASK_ID = "tests.apply_facade.write_path"
_PATH_TASK_SOURCE = """
def write_path(repo, path: str, text: str):
    repo.write(path, text.encode())
    return {"path": path}
"""


def _prepare(ws):
    ws.tasks.register_source(
        task_id=_PATH_TASK_ID,
        module="tests_apply_facade_tasks",
        source_text=_PATH_TASK_SOURCE,
        entrypoint="write_path",
        may_default="ReadWrite",
    )
    repo = seed_selected_workspace(ws)
    return ws.tasks.task(_PATH_TASK_ID), repo


def _run(task, repo, path: str, text: str):
    return task.run(repo=copy_git_repo(repo), args={"path": path, "text": text}, placement="advisory")


def test_facade_apply_merges_disjoint_delta_with_authority_evidence() -> None:
    """W2.4 (ii)+(v)+(vi): facade apply on an advanced parent records D7 evidence end-to-end."""
    with demo_workspace(None, keep=False) as ws:
        task, repo = _prepare(ws)
        run_a = _run(task, repo, "a.txt", "A")
        run_b = _run(task, repo, "b.txt", "B")
        out_b = run_b.output()
        ws.select(run_a.output())

        result = ws.apply(out_b.refresh())
        assert result.settlement.action == "applied"
        assert result.settlement.applied_head != result.settlement.candidate_head  # non-degenerate
        # The facade hydrated the authority lane from the recorded run context (D7).
        assert result.authority_outcome == "allowed"
        assert result.settlement.authority_operation_id == result.authority_operation_id

        # Read-side round-trip: state + joined settlement evidence carry the application.
        refreshed = out_b.refresh()
        assert refreshed.state == "applied"
        evidence = refreshed.settlement_evidence()
        assert evidence.settlement_action == "applied"
        assert evidence.authority_outcome == "allowed"
        assert evidence.authority_operation_id == result.authority_operation_id
        assert evidence.permission_plan_digest is not None


def test_facade_apply_overlap_refusal_names_paths_and_recovery_verbs() -> None:
    """W2.4 (iii facade leg): the D2 overlap refusal surfaces as WorkspaceControlError with paths."""
    with demo_workspace(None, keep=False) as ws:
        task, repo = _prepare(ws)
        run_a = _run(task, repo, "a.txt", "A")
        run_c = _run(task, repo, "a.txt", "C")  # same path: overlaps after A is selected
        out_c = run_c.output()
        ws.select(run_a.output())

        with pytest.raises(WorkspaceControlError, match=r"overlap.*a\.txt|a\.txt.*overlap") as excinfo:
            ws.apply(out_c.refresh())
        message = str(excinfo.value)
        assert "release/discard" in message  # the recovery path is named, not implied
        assert out_c.refresh().state == "unconsumed"  # refusal consumed nothing


def test_facade_apply_degenerate_matches_select_head_but_records_application() -> None:
    """W2.4 (i): unmoved parent — same head select would produce, but an `applied` receipt (D1a)."""
    with demo_workspace(None, keep=False) as ws:
        task, repo = _prepare(ws)
        run = _run(task, repo, "only.txt", "ONE")
        output = run.output()

        result = output.apply()  # RunOutput.apply mirror (W2.2)
        assert result.settlement.action == "applied"
        # Degenerate: the published head IS the candidate head — exactly what select publishes.
        assert result.settlement.applied_head == result.settlement.candidate_head
        assert output.refresh().state == "applied"


def test_facade_apply_changeset_view_is_invariant_across_settlement() -> None:
    """W2.4 (xii): the run's world-output view is identical before and after apply."""
    with demo_workspace(None, keep=False) as ws:
        task, repo = _prepare(ws)
        run_a = _run(task, repo, "a.txt", "A")
        run_b = _run(task, repo, "b.txt", "B")
        out_b = run_b.output()
        ws.select(run_a.output())

        before = out_b.refresh().changeset().changed_paths
        ws.apply(out_b.refresh())
        after = out_b.refresh().changeset().changed_paths
        assert before == after == ("b.txt",)


def test_facade_apply_fails_closed_when_parent_advances_mid_apply() -> None:
    """W2.4 (xi): a stale parent_world_before is never published over (the publish-CAS pin).

    Simulated, not raced (T1 G5): the parent advances via a sibling select injected between
    apply's parent-world read and its publication (VcsCore._lock is an RLock, so the same-thread
    nested settlement is legal); the apply must fail closed and consume nothing.
    """
    with demo_workspace(None, keep=False) as ws:
        task, repo = _prepare(ws)
        run_a = _run(task, repo, "a.txt", "A")
        run_b = _run(task, repo, "b.txt", "B")
        out_a, out_b = run_a.output(), run_b.output()

        from vcs_core import _retained_output_application as app_mod

        real_merge = app_mod._three_way_merge
        state = {"fired": False}

        def merge_then_advance(git_repo, basis, current, cand):
            merged = real_merge(git_repo, basis, current, cand)
            if not state["fired"]:
                state["fired"] = True
                ws.select(out_a.refresh())  # the parent advances AFTER apply read its world
            return merged

        app_mod._three_way_merge = merge_then_advance
        try:
            # B's basis == current parent here, so apply is degenerate unless we force the
            # three-way path: select A first? No — then the injection can't advance further
            # in a conflicting way. Instead: apply B onto the un-advanced parent is degenerate
            # (no merge call), so drive the non-degenerate path by selecting a third run.
            run_c = _run(task, repo, "c.txt", "C")
            ws.select(run_c.output())  # parent advances past A's and B's basis
            with pytest.raises(Exception, match=r"already settled|did not publish|input world|drift|advanced"):
                ws.apply(out_b.refresh())
        finally:
            app_mod._three_way_merge = real_merge
        # Nothing was consumed: B is still settleable once the world stabilizes.
        assert out_b.refresh().state == "unconsumed"
        result = ws.apply(out_b.refresh())
        assert result.settlement.action == "applied"


def test_cli_apply_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """W2.4 (viii): `shepherd run apply <exact-run-ref>` settles and renders the applied receipt."""
    import json

    from click.testing import CliRunner

    from shepherd_dialect import cli

    root = tmp_path / "ws"
    with demo_workspace(str(root), keep=True) as ws:
        task, repo = _prepare(ws)
        run_a = _run(task, repo, "a.txt", "A")
        run_b = _run(task, repo, "b.txt", "B")
        ws.select(run_a.output())
        apply_ref = run_b.run_ref

    monkeypatch.chdir(root)
    runner = CliRunner()

    # Mutation verbs demand exact run identity — selectors refuse (existing contract, F6).
    latest = runner.invoke(cli.main, ["run", "apply", "@latest"])
    assert latest.exit_code != 0
    assert "exact run identity" in latest.output

    result = runner.invoke(cli.main, ["run", "apply", apply_ref, "--binding", "workspace"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["settlement"]["action"] == "applied"
    assert payload["settlement"]["applied_head"] != payload["settlement"]["candidate_head"]

    second = runner.invoke(cli.main, ["run", "apply", apply_ref])
    assert second.exit_code != 0
    assert "unconsumed" in second.output or "already settled" in second.output


def test_facade_guard_gitrepo_noun_stays_verb_free() -> None:
    """W2.4 (ix): `apply` lives on outputs/workspace — never on the GitRepo value noun (G6)."""
    import shepherd as sp

    assert not hasattr(sp.GitRepo, "apply")
    assert not hasattr(sp.GitRepo, "write")
    assert not hasattr(sp.GitRepo, "run")


def test_workspace_module_settlement_kind_map_matches_authority_routes() -> None:
    """The dialect verb→kind map and the vcs-core kind→route map stay total together (g10 seam)."""
    from vcs_core._authority import AUTHORITY_ROUTE_BY_TRANSACTION_KIND

    kinds = {kind for _, kind in workspace_module.ShepherdWorkspace._MUTATING_SETTLEMENT_KINDS.values()}
    assert kinds <= set(AUTHORITY_ROUTE_BY_TRANSACTION_KIND)
    for kind in kinds:
        assert AUTHORITY_ROUTE_BY_TRANSACTION_KIND[kind] == kind
