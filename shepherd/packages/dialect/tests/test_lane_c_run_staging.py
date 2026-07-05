"""P-030 Lane C LC-3f — the multi-binding run staging seam (signature → per-binding surface).

`_stage_multi_binding_run` is the fenced-path staging function: it joins the signature's per-param
grants to the named bindings, resolves the S1/S2 per-binding decision, and produces the two root
representations a multi-binding run needs — the *absolute* `BindingRootGrant` sequence for the jail
install, and the *working-path-relative* `ConfinedBindingAuthority` tuple for per-binding handle
injection. The public `run(bindings=...)` fence stays up through LC-3, so these exercise the private
helpers directly and try to *refute*:

  - the flagship (docs:RO / backend:RW) stages the wrong authority or root shape;
  - the staging path reads the run-wide scalar (it must use the non-collapsing view — S2);
  - a bound root that is not strictly inside the working path is staged anyway (fail-closed);
  - an in-process per-binding handle lets a ReadOnly binding write (it must refuse at the handle).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from shepherd_dialect.confinement import lower_grants_to_confinement
from shepherd_dialect.workspace_control import ReadOnly, ReadWrite
from shepherd_dialect.workspace_control._confined_task_executor import ConfinedBindingAuthority
from shepherd_dialect.workspace_control.authority import gitrepo_grant_descriptor_from_public_grant
from shepherd_dialect.workspace_control.errors import WorkspaceControlError
from shepherd_dialect.workspace_control.may import HeterogeneousBindingAuthorityError
from shepherd_dialect.workspace_control.workspace import (
    _confined_multi_binding_provider,
    _in_process_binding_carriers,
    _relativize_bound_root,
    _stage_multi_binding_run,
)

if TYPE_CHECKING:
    from pathlib import Path


def _grant(param: str, grant: object):
    return gitrepo_grant_descriptor_from_public_grant(grant, grant_ref=f"signature:{param}")


def _signature(*params: tuple[str, object]) -> dict:
    """A signature schema carrying one `May[GitRepo, ...]` grant per named parameter."""
    return {
        "parameters": [{"name": name, "gitrepo_grant": _grant(name, grant).to_descriptor()} for name, grant in params]
    }


def _subdir(ws: Path, name: str) -> str:
    d = ws / name
    d.mkdir(parents=True, exist_ok=True)
    return os.path.realpath(str(d))


# --- staging happy path (the flagship) ------------------------------------------------------


def test_stage_flagship_docs_ro_backend_rw(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    binding_roots = {"docs": _subdir(ws, "docs"), "backend": _subdir(ws, "backend")}
    staging = _stage_multi_binding_run(
        signature_schema=_signature(("docs", ReadOnly), ("backend", ReadWrite)),
        binding_roots=binding_roots,
        task_default="Permissive",
        requested_may=None,
        workspace_path=ws,
    )
    # S2: per-binding authority preserved, never collapsed to one run-wide scalar.
    assert staging.decision.repo_authority_by_binding() == {"docs": "readonly", "backend": "readwrite"}
    # ConfinedBindingAuthority tuple — sorted by name, param == binding == parameter name, roots relativized.
    assert staging.binding_authorities == (
        ConfinedBindingAuthority(param="backend", binding="backend", authority="readwrite", root="backend"),
        ConfinedBindingAuthority(param="docs", binding="docs", authority="readonly", root="docs"),
    )
    # The BindingRootGrant sequence carries ABSOLUTE roots; the jail lowering keeps only the RW root.
    assert lower_grants_to_confinement(list(staging.binding_grants)).writable_roots == (binding_roots["backend"],)


def test_stage_readonly_ceiling_clamps_every_binding(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    binding_roots = {"docs": _subdir(ws, "docs"), "backend": _subdir(ws, "backend")}
    # Both request ReadWrite, but a whole-run ReadOnly ceiling (S1) must clamp both to read-only.
    staging = _stage_multi_binding_run(
        signature_schema=_signature(("docs", ReadWrite), ("backend", ReadWrite)),
        binding_roots=binding_roots,
        task_default="ReadOnly",
        requested_may=None,
        workspace_path=ws,
    )
    assert staging.decision.repo_authority_by_binding() == {"docs": "readonly", "backend": "readonly"}
    assert {a.authority for a in staging.binding_authorities} == {"readonly"}
    assert lower_grants_to_confinement(list(staging.binding_grants)).writable_roots == ()


def test_stage_never_reads_the_run_wide_scalar(tmp_path: Path) -> None:
    """The S2 tripwire proof: staging a heterogeneous run succeeds, and the run-wide scalar it
    deliberately never touched would in fact raise — so staging cannot have collapsed authority."""
    ws = tmp_path / "ws"
    ws.mkdir()
    binding_roots = {"docs": _subdir(ws, "docs"), "backend": _subdir(ws, "backend")}
    staging = _stage_multi_binding_run(
        signature_schema=_signature(("docs", ReadOnly), ("backend", ReadWrite)),
        binding_roots=binding_roots,
        task_default="Permissive",
        requested_may=None,
        workspace_path=ws,
    )
    with pytest.raises(HeterogeneousBindingAuthorityError):
        _ = staging.decision.repo_authority


# --- root relativization (fail-closed) ------------------------------------------------------


def test_relativize_returns_posix_subroot(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    assert _relativize_bound_root(_subdir(ws, "docs"), workspace_path=ws) == "docs"
    assert _relativize_bound_root(_subdir(ws, "pkgs/backend"), workspace_path=ws) == "pkgs/backend"


def test_relativize_refuses_root_equal_to_working_path(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(WorkspaceControlError, match="strictly inside"):
        _relativize_bound_root(os.path.realpath(str(ws)), workspace_path=ws)


def test_relativize_refuses_root_outside_working_path(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(WorkspaceControlError, match="strictly inside"):
        _relativize_bound_root(os.path.realpath(str(outside)), workspace_path=ws)


def test_stage_fails_closed_on_root_outside_working_path(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(WorkspaceControlError, match="strictly inside"):
        _stage_multi_binding_run(
            signature_schema=_signature(("docs", ReadOnly)),
            binding_roots={"docs": os.path.realpath(str(outside))},
            task_default="Permissive",
            requested_may=None,
            workspace_path=ws,
        )


# --- in-process per-binding handle injection ------------------------------------------------


def _authorities() -> tuple[ConfinedBindingAuthority, ...]:
    return (
        ConfinedBindingAuthority(param="docs", binding="docs", authority="readonly", root="docs"),
        ConfinedBindingAuthority(param="backend", binding="backend", authority="readwrite", root="backend"),
    )


def test_in_process_handles_inject_by_param_with_own_root_and_authority(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    handles = _in_process_binding_carriers(working_path=work, binding_authorities=_authorities())
    assert set(handles) == {"docs", "backend"}
    handles["backend"].write("candidate.py", b"# fix\n")
    # the write landed under the backend handle's OWN root, not the working path root
    assert (work / "backend" / "candidate.py").read_bytes() == b"# fix\n"
    assert not (work / "candidate.py").exists()


def test_in_process_readonly_handle_write_refused(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    handles = _in_process_binding_carriers(working_path=work, binding_authorities=_authorities())
    with pytest.raises(PermissionError, match="not permitted under authority='readonly'"):
        handles["docs"].write("SHOULD_NOT_LAND.md", b"nope")
    assert not (work / "docs" / "SHOULD_NOT_LAND.md").exists()


def test_in_process_param_collision_fails_closed(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    colliding = (
        ConfinedBindingAuthority(param="repo", binding="a", authority="readwrite", root="a"),
        ConfinedBindingAuthority(param="repo", binding="b", authority="readwrite", root="b"),
    )
    with pytest.raises(WorkspaceControlError, match="collides"):
        _in_process_binding_carriers(working_path=work, binding_authorities=colliding)


def test_in_process_non_relative_root_fails_closed(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    escaped = (ConfinedBindingAuthority(param="p", binding="p", authority="readwrite", root="/etc"),)
    with pytest.raises(WorkspaceControlError, match="relative POSIX path"):
        _in_process_binding_carriers(working_path=work, binding_authorities=escaped)


def test_in_process_empty_bindings_fails_closed(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(WorkspaceControlError, match="at least one binding"):
        _in_process_binding_carriers(working_path=work, binding_authorities=())


# --- confined provider construction ---------------------------------------------------------


def test_confined_multi_binding_provider_carries_binding_authorities_not_repo_authority(tmp_path: Path) -> None:
    provider = _confined_multi_binding_provider(
        artifact_payload={"files": [], "entrypoint": {"module": "m", "qualname": "q"}},
        args={"issue": "#142"},
        binding_authorities=_authorities(),
    )
    assert provider.repo_authority is None
    assert provider.binding_authorities == _authorities()
    # The staged request carries the per-binding `bindings` shape, never the single `repo` collapse.
    import json

    request = json.loads(provider._stage_request(tmp_path).read_text(encoding="utf-8"))
    assert "repo" not in request
    assert [b["param"] for b in request["bindings"]] == ["docs", "backend"]
    assert request["kwargs"] == {"issue": "#142"}


def test_run_driver_rebase_refuses_escaping_relative_grant_roots() -> None:
    # Defense in depth at the install seam (Lane C LC-4): the facade's staging already refuses
    # `..`/`.` sub-roots, but the driver must not trust its (Python-only) caller — a joined `..`
    # would jail-authorize a subtree OUTSIDE the run clone.
    import pytest

    from shepherd_dialect.confinement import BindingRootGrant
    from shepherd_dialect.run_driver import _rebased_binding_grants

    for bad in ("../outside", "a/../../b", ".", ""):
        with pytest.raises(ValueError, match="refusing to rebase"):
            _rebased_binding_grants([BindingRootGrant(binding="b", root=bad, writable=True)], "/tmp/clone")
    ok = _rebased_binding_grants([BindingRootGrant(binding="b", root="backend", writable=True)], "/tmp/clone")
    assert ok[0].root == "/tmp/clone/backend"
