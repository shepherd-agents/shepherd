"""Cross-package authority integration tests."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from shepherd_runtime.effects.policy import Match
from vcs_core import Store, VcsCore, build_builtin_substrate_context
from vcs_core._permission_plan_evidence import permission_plan_digest
from vcs_core.substrates import FilesystemSubstrate, MarkerSubstrate
from vcs_core.types import FileState, normalize_git_filemode

if TYPE_CHECKING:
    from vcs_core._authority import AuthorityOutcome, GitRepoAuthorityRequest

# The authority merge seam requires PermissionPlan monitor evidence (fail-closed
# since 2026-07-01). This carrier-diff descriptor mirrors the green pattern in
# vcs-core's own test_authority_merge.py; the digests are opaque test tokens.
_EFFECTIVE_MATCH_DIGEST = "test-match-integration-effective-match"
_AUTHORITY_SURFACE_PLAN_DIGEST = "test-match-integration-surface-plan"
_PERMISSION_PLAN_DESCRIPTOR = {
    "schema": "shepherd.permission-plan.v1",
    "fallback": "enforce",
    "assignments": [
        {
            "monitor": "carrier_check_at_commit",
            "timing": "commit",
            "route": "carrier_diff",
            "completeness_basis": "test prepared filesystem carrier diff evidence",
            "tamper_basis": "test coordinator-owned authority merge",
            "confinement": None,
            "evidence": {
                "effective_match_digest": _EFFECTIVE_MATCH_DIGEST,
                "authority_surface_plan_digest": _AUTHORITY_SURFACE_PLAN_DIGEST,
            },
        }
    ],
}
_PERMISSION_PLAN_DIGEST = permission_plan_digest(_PERMISSION_PLAN_DESCRIPTOR)


def _authority_plan_kwargs() -> dict[str, object]:
    return {
        "effective_match_digest": _EFFECTIVE_MATCH_DIGEST,
        "authority_surface_plan_digest": _AUTHORITY_SURFACE_PLAN_DIGEST,
        "permission_plan_digest": _PERMISSION_PLAN_DIGEST,
        "permission_plan_descriptor": _PERMISSION_PLAN_DESCRIPTOR,
    }


class _OverlayBackend:
    def __init__(self) -> None:
        self.layers: dict[str, dict[str, FileState | None]] = {}
        self.committed: list[tuple[str, str | None]] = []
        self.discarded: list[str] = []

    def create_layer(self, scope_id: str, *, parent_scope_id: str | None) -> None:
        del parent_scope_id
        self.layers.setdefault(scope_id, {})

    def has_layer(self, scope_id: str) -> bool:
        return scope_id in self.layers

    def diff_layer(self, scope_id: str) -> list[tuple[str, bytes | None, int]]:
        layer = self.layers.get(scope_id, {})
        return [
            (path, state.content, state.mode) if state is not None else (path, None, 0) for path, state in layer.items()
        ]

    def commit_layer(self, scope_id: str, *, into_scope_id: str | None) -> None:
        self.committed.append((scope_id, into_scope_id))

    def discard_layer(self, scope_id: str) -> None:
        self.discarded.append(scope_id)
        self.layers.pop(scope_id, None)

    def write_file(self, scope_id: str, path: str, content: bytes, *, mode: int = 0o100644) -> None:
        self.layers.setdefault(scope_id, {})[path] = FileState(content, normalize_git_filemode(mode))

    def read_file_state(self, scope_id: str, path: str) -> FileState:
        state = self.layers[scope_id][path]
        assert state is not None
        return state

    def read_file(self, scope_id: str, path: str) -> bytes:
        return self.read_file_state(scope_id, path).content

    def delete_file(self, scope_id: str, path: str) -> None:
        self.layers.setdefault(scope_id, {})[path] = None

    def push_layer(self, scope_id: str | None = None) -> None:
        del scope_id

    def working_path(self, scope_id: str) -> Path:
        return Path("/virtual") / scope_id

    def deactivate(self) -> None:
        pass


def _make_mg(tmp_path: Path, backend: _OverlayBackend) -> VcsCore:
    store = Store(str(tmp_path / "ws" / ".vcscore"))
    context = build_builtin_substrate_context(store)
    mg = VcsCore(
        str(tmp_path / "ws"),
        substrates=[
            MarkerSubstrate(context),
            FilesystemSubstrate(context, backend=backend),
        ],
        store=store,
    )
    mg.activate()
    return mg


def test_authority_merge_accepts_shepherd_match_decisions(tmp_path: Path) -> None:
    """Shepherd Match decisions can drive the VcsCore authority merge seam."""
    backend = _OverlayBackend()
    mg = _make_mg(tmp_path, backend)
    try:
        effective = Match.field("binding_ref", "eq", "backend") & Match.field("path", "startswith", "src/app/")

        def decide(request: GitRepoAuthorityRequest) -> AuthorityOutcome:
            if effective.matches(request.match_view.as_mapping()):
                return "allowed"
            return "denied"

        allowed = mg.fork(mg.ground, "match-allowed", hints={"isolated": True})
        backend.write_file(allowed.name, "backend/src/app/main.py", b"ok\n")
        allowed_result = mg.merge_with_authority(
            allowed,
            mg.ground,
            binding_roots={"backend": "backend", "docs": "docs"},
            decide=decide,
            **_authority_plan_kwargs(),
        )

        denied = mg.fork(mg.ground, "match-denied", hints={"isolated": True})
        backend.write_file(denied.name, "docs/nope.py", b"no\n")
        denied_result = mg.merge_with_authority(
            denied,
            mg.ground,
            binding_roots={"backend": "backend", "docs": "docs"},
            decide=decide,
            **_authority_plan_kwargs(),
        )

        assert allowed_result.outcome == "allowed"
        assert denied_result.outcome == "denied"
        assert mg.store.read_workspace_file(mg.ground.ref, "backend/src/app/main.py") == b"ok\n"
        assert mg.store.read_workspace_file(mg.ground.ref, "docs/nope.py") is None
    finally:
        mg.deactivate(warn_on_open_scopes=False)
