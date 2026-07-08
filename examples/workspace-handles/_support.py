"""Private helpers for the workspace-handle examples."""

# ruff: noqa: INP001

from __future__ import annotations

import json
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from shepherd_dialect.run_driver import ShepherdRunDriver
from shepherd_dialect.workspace_control import (
    RunOutput,
    ShepherdRunLedgerDriver,
    ShepherdTaskArtifactDriver,
    ShepherdTaskLedgerDriver,
    ShepherdWorkspace,
)
from shepherd_runtime.nucleus import GitRepo, GitRepoBasis
from vcs_core import FilesystemSubstrate, MarkerSubstrate, Store, VcsCore, build_builtin_substrate_context
from vcs_core.runtime_substrate import TaskTraceSubstrateDriver

if TYPE_CHECKING:
    from collections.abc import Iterator

CANDIDATE_TASK_ID = "examples.workspace_handles.propose"
CANDIDATE_SOURCE = """
from shepherd_runtime.nucleus import GitRepo


def propose(repo: GitRepo, label: str, score: int, accepted: bool = False):
    status = "accepted" if accepted else "rejected"
    repo.write("candidate.txt", f"{score}:{label}:{status}\\n".encode())
    return {"label": label, "score": score, "accepted": accepted}
"""


@contextmanager
def demo_workspace(workspace_path: str | None, *, keep: bool) -> Iterator[ShepherdWorkspace]:
    """Open a demo workspace, cleaning up generated temporary workspaces."""
    generated = workspace_path is None
    if workspace_path is None:
        root = Path(tempfile.mkdtemp(prefix="shepherd-example-"))
    else:
        root = Path(workspace_path).expanduser().resolve()
    workspace = open_workspace(root)
    try:
        yield workspace
    finally:
        workspace.close()
        if generated and not keep:
            shutil.rmtree(root, ignore_errors=True)


def open_workspace(root: Path) -> ShepherdWorkspace:
    root.mkdir(parents=True, exist_ok=True)
    store = Store(str(root / ".vcscore"))
    context = build_builtin_substrate_context(store=store, workspace=root, config={"backend": "clonefile"})
    mg = VcsCore(
        str(root),
        substrates=[
            MarkerSubstrate(context),
            FilesystemSubstrate(context),
            TaskTraceSubstrateDriver(),
            ShepherdTaskLedgerDriver(),
            ShepherdTaskArtifactDriver(),
            ShepherdRunLedgerDriver(),
            ShepherdRunDriver(),
        ],
        store=store,
    )
    mg.activate()
    return ShepherdWorkspace(
        mg,
        trace_store_path=root / ".vcscore" / "shepherd" / "trace.sqlite",
        workspace_path=root,
    )


def seed_selected_workspace(workspace: ShepherdWorkspace) -> GitRepo:
    workspace.mg.exec("filesystem", "write", scope=workspace.mg.ground, path="base.txt", content=b"base\n")
    return workspace.git_repo()


def register_candidate_task(workspace: ShepherdWorkspace) -> None:
    workspace.tasks.register_source(
        task_id=CANDIDATE_TASK_ID,
        module="examples_workspace_handles_tasks",
        source_text=CANDIDATE_SOURCE,
        entrypoint="propose",
        may_default="ReadWrite",
    )


def copy_git_repo(repo: GitRepo) -> GitRepo:
    return GitRepo.from_payload(json.loads(json.dumps(repo.to_payload())))


def candidate_text(output: RunOutput) -> str:
    value = output.changeset().read_file("candidate.txt")
    if value is None:
        raise RuntimeError("candidate output did not contain candidate.txt")
    return value[0].decode("utf-8")


def changeset_stat(output: RunOutput) -> dict[str, object]:
    """Review one candidate through its `Changeset` — the settlement-spelling judging surface.

    Goal 15's capstone judges N retained candidates *by their changesets* (what each one
    changed), not by a rendered artifact the caller trusts blindly. `read_file` reads through
    the same changeset custody.
    """
    stat = output.changeset().stat()
    return {"changed_paths": list(stat.changed_paths), "changed_path_count": stat.changed_path_count}


def enforcement_of(run: object) -> str:
    """The honestly-recorded enforcement for a run (`"jail"` or `"advisory"`; A6).

    Handle-bearing runs default to `placement="auto"`: the native syscall jail on a jail-capable
    host, advisory in the in-process dev column. The recorded value is what actually happened —
    never a hard-coded assumption.
    """
    return run.record.enforcement  # type: ignore[attr-defined]


def basis_summary(basis: GitRepoBasis) -> dict[str, str]:
    return {
        "world_oid": basis.world_oid,
        "store_id": basis.store_id,
        "resource_id": basis.resource_id,
        "head": basis.head,
    }


def output_summary(output: RunOutput) -> dict[str, object]:
    authority = output.run_authority()
    policy = output.settlement_policy()
    evidence = output.settlement_evidence()
    return {
        "output_id": output.output_id,
        "run_ref": output.owner.run_id,
        "state": output.refresh().state,
        "text": candidate_text(output),
        "changeset": changeset_stat(output),
        "authority": {
            "effective_may": authority.effective_may,
            "effective_grant_digest": authority.effective_grant_digest,
            "effective_match_digest": authority.effective_match_digest,
        },
        "settlement_policy": {
            "custody_owner": policy.custody_owner,
            "consume_once": policy.consume_once,
            "verbs": list(policy.settlement_verbs),
        },
        "settlement_evidence": {
            "action": evidence.settlement_action,
            "authority_outcome": evidence.authority_outcome,
            "permission_plan_digest": evidence.permission_plan_digest,
        },
    }
