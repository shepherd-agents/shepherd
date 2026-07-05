"""P-030 Lane C LC-3e — the confined request/runner protocol, generalized to named bindings.

The confined runner is the in-body enforcement layer beneath the syscall jail: each injected
handle carries its own clamped authority and its own sub-root. These tests run the real runner
subprocess (the same argv shape the executor launches, minus the jail) and try to *refute*:

  - the single-binding ``repo`` request changed shape or behavior (it must stay byte-identical);
  - a per-binding RO handle can write (it must refuse at the handle layer, before any jail);
  - a handle can write outside its own root (relative-path validation);
  - a malformed request (both/neither of repo|bindings, param collisions) runs anyway.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from shepherd_dialect.workspace_control._confined_task_executor import (
    ConfinedBindingAuthority,
    ConfinedRootTaskProvider,
    _confined_task_runner_entrypoint_path,
)

_TASK_SOURCE = """
def two_repo_fix(docs, backend, note="n"):
    backend.write("candidate.py", b"# fix\\n")
    return {"note": note, "backend_binding": backend.binding, "docs_authority": docs.authority}


def docs_writer(docs, backend):
    docs.write("SHOULD_NOT_LAND.md", b"nope")
    return {}


def single(repo, note="n"):
    repo.write("single.txt", b"ok")
    return {"note": note, "binding": repo.binding}
"""


def _stage(tmp_path: Path, request: dict) -> tuple[Path, Path]:
    source_root = tmp_path / "src"
    source_root.mkdir(exist_ok=True)
    (source_root / "lanec_protocol_tasks.py").write_text(_TASK_SOURCE, encoding="utf-8")
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    request = {
        "schema": "shepherd.workspace_control.confined_task_request.v1",
        "source_root": str(source_root),
        **request,
    }
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request, sort_keys=True), encoding="utf-8")
    return request_path, workdir


def _run(request_path: Path, workdir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(_confined_task_runner_entrypoint_path()), str(request_path)],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _entry(qualname: str) -> dict:
    return {"entrypoint": {"module": "lanec_protocol_tasks", "qualname": qualname}}


def _bindings(*entries: tuple[str, str, str]) -> list[dict]:
    return [{"param": p, "binding": p, "authority": a, "root": r} for p, a, r in entries]


# --- per-binding protocol -------------------------------------------------------------------


def test_bindings_inject_by_param_name_with_own_roots_and_authorities(tmp_path: Path) -> None:
    request_path, workdir = _stage(
        tmp_path,
        {
            **_entry("two_repo_fix"),
            "kwargs": {"note": "flagship"},
            "bindings": _bindings(("docs", "readonly", "docs"), ("backend", "readwrite", "backend")),
        },
    )
    proc = _run(request_path, workdir)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["result"] == {"note": "flagship", "backend_binding": "backend", "docs_authority": "readonly"}
    # the write landed under the backend handle's OWN root, not the working path root
    assert (workdir / "backend" / "candidate.py").exists()
    assert not (workdir / "candidate.py").exists()


def test_readonly_binding_write_refused_at_the_handle_layer(tmp_path: Path) -> None:
    request_path, workdir = _stage(
        tmp_path,
        {
            **_entry("docs_writer"),
            "bindings": _bindings(("docs", "readonly", "docs"), ("backend", "readwrite", "backend")),
        },
    )
    proc = _run(request_path, workdir)
    assert proc.returncode != 0
    error = json.loads(proc.stderr)
    assert error["type"] == "PermissionError"
    assert "docs" in error["message"]
    assert not (workdir / "docs" / "SHOULD_NOT_LAND.md").exists()


def test_binding_root_must_be_relative(tmp_path: Path) -> None:
    request_path, workdir = _stage(
        tmp_path,
        {
            **_entry("two_repo_fix"),
            "bindings": _bindings(("docs", "readonly", "docs"), ("backend", "readwrite", "/etc")),
        },
    )
    proc = _run(request_path, workdir)
    assert proc.returncode != 0
    assert "relative POSIX path" in json.loads(proc.stderr)["message"]


def test_param_collision_with_kwargs_fails_closed(tmp_path: Path) -> None:
    request_path, workdir = _stage(
        tmp_path,
        {
            **_entry("two_repo_fix"),
            "kwargs": {"backend": "smuggled"},
            "bindings": _bindings(("docs", "readonly", "docs"), ("backend", "readwrite", "backend")),
        },
    )
    proc = _run(request_path, workdir)
    assert proc.returncode != 0
    assert "collides" in json.loads(proc.stderr)["message"]


def test_exactly_one_of_repo_or_bindings(tmp_path: Path) -> None:
    both = {
        **_entry("single"),
        "repo": {"binding": "workspace", "authority": "readwrite"},
        "bindings": _bindings(("backend", "readwrite", "backend")),
    }
    request_path, workdir = _stage(tmp_path, both)
    assert _run(request_path, workdir).returncode != 0
    request_path, workdir = _stage(tmp_path, _entry("single"))  # neither
    assert _run(request_path, workdir).returncode != 0


# --- single-binding regression (byte-identical v0.1 shape) ------------------------------------


def test_single_binding_request_unchanged(tmp_path: Path) -> None:
    request_path, workdir = _stage(
        tmp_path,
        {**_entry("single"), "kwargs": {"note": "v01"}, "repo": {"binding": "workspace", "authority": "readwrite"}},
    )
    proc = _run(request_path, workdir)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["result"] == {"note": "v01", "binding": "workspace"}
    assert (workdir / "single.txt").exists()


# --- executor request staging ------------------------------------------------------------------


def test_stage_request_single_binding_shape_is_byte_identical(tmp_path: Path) -> None:
    provider = ConfinedRootTaskProvider(
        artifact_payload={"files": [], "entrypoint": {"module": "m", "qualname": "q"}},
        kwargs={"a": 1},
        repo_authority="readonly",
    )
    request = json.loads(provider._stage_request(tmp_path).read_text(encoding="utf-8"))
    assert request["repo"] == {"binding": "workspace", "authority": "readonly"}
    assert "bindings" not in request


def test_stage_request_per_binding_shape(tmp_path: Path) -> None:
    provider = ConfinedRootTaskProvider(
        artifact_payload={"files": [], "entrypoint": {"module": "m", "qualname": "q"}},
        kwargs={},
        binding_authorities=(
            ConfinedBindingAuthority(param="docs", binding="docs", authority="readonly", root="docs"),
            ConfinedBindingAuthority(param="backend", binding="backend", authority="readwrite", root="backend"),
        ),
    )
    request = json.loads(provider._stage_request(tmp_path).read_text(encoding="utf-8"))
    assert "repo" not in request
    assert request["bindings"] == [
        {"param": "docs", "binding": "docs", "authority": "readonly", "root": "docs"},
        {"param": "backend", "binding": "backend", "authority": "readwrite", "root": "backend"},
    ]


def test_stage_request_refuses_ambiguous_authority_shape(tmp_path: Path) -> None:
    neither = ConfinedRootTaskProvider(
        artifact_payload={"files": [], "entrypoint": {"module": "m", "qualname": "q"}},
        kwargs={},
    )
    neither_root = tmp_path / "neither"
    neither_root.mkdir()
    with pytest.raises(RuntimeError, match="exactly one"):
        neither._stage_request(neither_root)
    both = ConfinedRootTaskProvider(
        artifact_payload={"files": [], "entrypoint": {"module": "m", "qualname": "q"}},
        kwargs={},
        repo_authority="readonly",
        binding_authorities=(ConfinedBindingAuthority(param="p", binding="p", authority="readonly", root="p"),),
    )
    both_root = tmp_path / "both"
    both_root.mkdir()
    with pytest.raises(RuntimeError, match="exactly one"):
        both._stage_request(both_root)
