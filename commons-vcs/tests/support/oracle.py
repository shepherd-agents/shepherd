"""Phase -1 oracle helpers reused by promoted GitBackend smoke tests."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

from commons_vcs import Edge, Object, Repo
from commons_vcs.canonical import CANONICAL_PREFIX

from support import profiles
from support.coordinator import PhaseMinus1Coordinator
from support.filesystem_substrate import FilesystemSubstrate

ORACLE = {
    "PARENT_COMMIT": "sha256:6facdc01e639d3d4ea9feb1599138d88afd591236e1f02f506bd6e90adce0537",
    "SHEPHERD_EFFECT": "sha256:62346f205434a6e0622ea686e7dc73ea0f1450fe618732377cf022e9a1c8a51e",
    "VCSCORE_COMMIT": "sha256:fd9ec56e6de4ad9bc0f1eb352d2024f2d7750e78d3115d1efbc3b5d8fe35a922",
    "SGC_RECEIPT": "sha256:44de863ce63b7380869b32d3432098a0561b05aa7c0868021c334b7ec9e70cd2",
    "TOOL_STDOUT": "sha256:fe9562d84a037e6fe860a3b43a87a53773536e4ea63c8e739e09353b80efa860",
}


def raw_bytes_digest(payload: bytes) -> str:
    """Phase -1 raw-byte digest convention."""
    h = hashlib.sha256()
    h.update(CANONICAL_PREFIX)
    h.update(payload)
    return f"sha256:{h.hexdigest()}"


def build_oracle_graph(backend=None) -> tuple[Repo, dict[str, str]]:
    if backend is None:
        repo = Repo(profiles=[profiles.vcscore.profile, profiles.shepherd.profile, profiles.sgc_stub.profile])
    else:
        repo = Repo(
            profiles=[profiles.vcscore.profile, profiles.shepherd.profile, profiles.sgc_stub.profile],
            backend=backend,
        )

    parent_commit = Object(
        schema_ref="vcscore/commit/v1",
        body={"workspace_tree": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b"},
        edges=(),
    )
    parent_id = repo.append(parent_commit)

    tool_stdout_digest = raw_bytes_digest(b"")
    shepherd_effect = Object(
        schema_ref="shepherd/effect/v1",
        body={
            "completed_at_ns": 1745798400012000000,
            "duration_ms": 12,
            "output": "",
            "output_bytes_len": 0,
            "output_digest": tool_stdout_digest,
            "params": {"command": "echo hello > /tmp/spike/hello.txt"},
            "provider_id": "claude-sonnet-4-6",
            "scope_id": "scope:phase-1/step-1",
            "started_at_ns": 1745798400000000000,
            "success": True,
            "task_name": "phase_minus_1_demo",
            "tool_call_id": "tooluse_phase1_001",
            "tool_name": "bash",
            "type": "tool_call_completed",
        },
        edges=(Edge("executed-against", parent_id),),
    )
    effect_id = repo.append(shepherd_effect)

    vcscore_commit = Object(
        schema_ref="vcscore/commit/v1",
        body={"workspace_tree": "1234567890abcdef1234567890abcdef12345678"},
        edges=(
            Edge("effect", effect_id),
            Edge("parent", parent_id),
        ),
    )
    vcscore_id = repo.append(vcscore_commit)

    sgc_receipt = Object(
        schema_ref="sgc/receipt/v1",
        body={
            "decision": "approve",
            "summary": "automated approval for phase -1 spike",
        },
        edges=(Edge("evidence", effect_id),),
    )
    sgc_id = repo.append(sgc_receipt)

    return repo, {
        "PARENT_COMMIT": parent_id,
        "SHEPHERD_EFFECT": effect_id,
        "VCSCORE_COMMIT": vcscore_id,
        "SGC_RECEIPT": sgc_id,
    }


def run_phase_b(repo: Repo, oracle: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    failures: list[str] = []
    new_digests: dict[str, str] = {}

    with (
        tempfile.TemporaryDirectory(prefix="phase_minus_1_workdir_") as wd_str,
        tempfile.TemporaryDirectory(prefix="phase_minus_1_gitdir_") as gd_str,
    ):
        workdir = Path(wd_str)
        git_dir = Path(gd_str)

        subprocess.run(["git", "init", "--bare", "--quiet", str(git_dir)], check=True, capture_output=True)
        (workdir / "hello.txt").write_bytes(b"hello\n")

        observation = FilesystemSubstrate(workdir, git_dir).capture()
        if observation.workspace_tree is None:
            failures.append("Phase B: substrate returned no workspace_tree")
            return failures, new_digests
        if len(observation.workspace_tree) not in (40, 64):
            failures.append(f"Phase B: workspace_tree wrong length: {observation.workspace_tree!r}")
        if observation.metadata.get("file_count") != 1:
            failures.append(f"Phase B: substrate metadata wrong file_count: {observation.metadata}")

        try:
            ls_tree = subprocess.run(
                ["git", "ls-tree", observation.workspace_tree],
                env={"GIT_DIR": str(git_dir), **os.environ},
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if "hello.txt" not in ls_tree:
                failures.append(f"Phase B: ls-tree on workspace_tree missing hello.txt: {ls_tree!r}")
        except subprocess.CalledProcessError as exc:
            failures.append(f"Phase B: git ls-tree failed: {exc.stderr}")

        try:
            new_commit_id = PhaseMinus1Coordinator(repo).append_commit(
                effect_id=oracle["SHEPHERD_EFFECT"],
                observation=observation,
                parent_id=oracle["PARENT_COMMIT"],
            )
            new_digests["PHASE_B_COMMIT"] = new_commit_id
        except Exception as exc:  # pragma: no cover - assertion path reports diagnostic
            failures.append(f"Phase B: coordinator.append_commit raised: {exc}")
            return failures, new_digests

    return failures, new_digests
