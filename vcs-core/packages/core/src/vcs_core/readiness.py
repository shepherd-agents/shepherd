"""JSON helpers for vcs-core readiness queries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from vcs_core import _cli_ipc
from vcs_core._query_readiness import MutationPrecondition, ReadinessRequest
from vcs_core.store import Store
from vcs_core.vcscore import VcsCore


def query_readiness_json(
    workspace: str | Path,
    request: dict[str, object] | ReadinessRequest | None = None,
) -> dict[str, object]:
    """Return vcs-core readiness as JSON-compatible data.

    The helper is intentionally small: callers consume the named readiness
    envelope, while vcs-core owns request normalization, inventory collection,
    policy, and locked admission for mutating commands.
    """
    workspace_path, repo_path = _resolve_workspace_and_repo_path(workspace)
    readiness_request = _coerce_request(request)
    session_info = _cli_ipc.live_session_info(str(repo_path))
    if session_info is not None:
        response = _cli_ipc.send_session_request(
            session_info,
            "query_readiness",
            {} if readiness_request is None else readiness_request.to_json(),
        )
        if not _cli_ipc.response_ok(response):
            raise RuntimeError(_cli_ipc.response_error(response))
        return _cli_ipc.response_result(response)

    store = Store.open_existing(str(repo_path))
    vcscore = VcsCore(str(workspace_path), store=store, allow_activate_init=False)
    return vcscore.query_readiness(readiness_request).to_json()


def revalidate_readiness_json(
    workspace: str | Path,
    request: dict[str, object] | ReadinessRequest,
    precondition: dict[str, object] | MutationPrecondition,
) -> dict[str, object]:
    """Revalidate an opaque readiness precondition through vcs-core."""
    workspace_path, repo_path = _resolve_workspace_and_repo_path(workspace)
    readiness_request = _coerce_required_request(request)
    session_info = _cli_ipc.live_session_info(str(repo_path))
    if session_info is not None:
        response = _cli_ipc.send_session_request(
            session_info,
            "revalidate_readiness",
            {
                "request": readiness_request.to_json(),
                "precondition": _precondition_to_json(precondition),
            },
        )
        if not _cli_ipc.response_ok(response):
            raise RuntimeError(_cli_ipc.response_error(response))
        return _cli_ipc.response_result(response)

    store = Store.open_existing(str(repo_path))
    vcscore = VcsCore(str(workspace_path), store=store, allow_activate_init=False)
    return vcscore.revalidate_readiness_precondition(readiness_request, precondition).to_json()


def _coerce_request(request: dict[str, object] | ReadinessRequest | None) -> ReadinessRequest | None:
    if request is None or isinstance(request, ReadinessRequest):
        return request
    return ReadinessRequest.from_json(request)


def _coerce_required_request(request: dict[str, object] | ReadinessRequest) -> ReadinessRequest:
    if isinstance(request, ReadinessRequest):
        return request
    return ReadinessRequest.from_json(request)


def _precondition_to_json(precondition: dict[str, object] | MutationPrecondition) -> dict[str, Any]:
    if isinstance(precondition, MutationPrecondition):
        return precondition.to_json()
    return dict(precondition)


def _resolve_workspace_and_repo_path(workspace: str | Path) -> tuple[Path, Path]:
    path = Path(workspace).expanduser().resolve()
    if path.name == ".vcscore":
        return path.parent, path
    current = path.parent if path.is_file() else path
    for candidate in (current, *current.parents):
        repo_path = candidate / ".vcscore"
        if repo_path.exists():
            return candidate, repo_path
    return path, path / ".vcscore"


__all__ = ["query_readiness_json", "revalidate_readiness_json"]
