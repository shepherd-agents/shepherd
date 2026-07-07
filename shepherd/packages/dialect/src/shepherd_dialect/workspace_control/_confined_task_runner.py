"""Subprocess runner for confined workspace-control task artifacts."""

from __future__ import annotations

import importlib
import inspect
import json
import sys
import traceback
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

_REQUEST_SCHEMA = "shepherd.workspace_control.confined_task_request.v1"
_RESULT_SCHEMA = "shepherd.workspace_control.confined_task_result.v1"
_ERROR_SCHEMA = "shepherd.workspace_control.confined_task_error.v1"


@dataclass(frozen=True)
class _ConfinedCarrierGitRepo:
    """Minimal artifact-runner GitRepo over one bound root inside the confined process cwd.

    The single-binding form roots at the working path (``binding="workspace"``); a Lane C
    per-binding handle roots at its bound subtree, so a write path is relative to *its own*
    root and the in-body authority check enforces that binding's clamped grant — the second
    enforcement layer beneath the syscall jail.
    """

    root: Path
    authority: str
    binding: str = "workspace"

    def write(self, path: str, content: bytes, *, mode: int = 0o100644) -> _ConfinedCarrierGitRepo:
        _validate_workspace_relative_path(path)
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        if self.authority != "readwrite":
            raise PermissionError(
                f"GitRepoHandle.write is not permitted under authority={self.authority!r} for binding {self.binding!r}"
            )
        if not isinstance(mode, int):
            raise TypeError("mode must be an int")
        target = self.root / PurePosixPath(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(mode)
        return self


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        _write_error(ValueError("usage: _confined_task_runner <request-json-path>"))
        return 2
    try:
        request = _read_request(Path(args[0]))
        source_root = _required_str(request, "source_root")
        entrypoint = _required_mapping(request, "entrypoint")
        module_name = _required_str(entrypoint, "module")
        qualname = _required_str(entrypoint, "qualname")
        kwargs = dict(_optional_mapping(request, "kwargs"))
        repo_payload = request.get("repo")
        bindings_payload = request.get("bindings")
        if (repo_payload is None) == (bindings_payload is None):
            raise ValueError("confined task request must carry exactly one of 'repo' or 'bindings'")
        sys.path.insert(0, source_root)
        module = importlib.import_module(module_name)
        task_body = _resolve_qualname(module, qualname)
        if not callable(task_body):
            raise TypeError(f"task artifact entrypoint {module_name}:{qualname} is not callable")
        if repo_payload is not None:
            # v0.1 single-binding: one whole-working-path handle, first positional argument.
            if not isinstance(repo_payload, Mapping):
                raise TypeError("confined task request field 'repo' must be an object")
            repo = _ConfinedCarrierGitRepo(
                root=Path.cwd(),
                authority=_required_str(repo_payload, "authority"),
                binding=_required_str(repo_payload, "binding"),
            )
            result = task_body(repo, **kwargs)
        else:
            # Lane C LC-3e/LC-3f: one handle per named binding, injected by parameter name,
            # each rooted at its own sub-root with its own clamped authority.
            handles = _binding_handles(bindings_payload, kwargs)
            result = task_body(**handles, **kwargs)
        sys.stdout.write(json.dumps({"schema": _RESULT_SCHEMA, "status": "ok", "result": _portable(result)}))
        sys.stdout.write("\n")
        return 0
    except BaseException as exc:  # noqa: BLE001 - report task/runtime failures to the parent runner.
        _write_error(exc)
        return 2


def _binding_handles(bindings_payload: object, kwargs: Mapping[str, Any]) -> dict[str, _ConfinedCarrierGitRepo]:
    """Build per-binding handles from a Lane C ``bindings`` request (fail-closed).

    Each entry names its task parameter, binding, clamped authority, and working-path-relative
    root. Param collisions (with ``kwargs`` or another binding) and non-relative roots refuse —
    a malformed request never runs with ambiguous or escaped authority.
    """
    if not isinstance(bindings_payload, list) or not bindings_payload:
        raise TypeError("confined task request field 'bindings' must be a non-empty array")
    handles: dict[str, _ConfinedCarrierGitRepo] = {}
    for raw in bindings_payload:
        if not isinstance(raw, Mapping):
            raise TypeError("each confined task binding entry must be an object")
        param = _required_str(raw, "param")
        binding = _required_str(raw, "binding")
        authority = _required_str(raw, "authority")
        root_rel = raw.get("root", "")
        if not isinstance(root_rel, str):
            raise TypeError("confined task binding field 'root' must be a string")
        if root_rel in {"", "."}:
            root = Path.cwd()
        else:
            _validate_workspace_relative_path(root_rel)
            root = Path.cwd() / PurePosixPath(root_rel)
        if param in kwargs or param in handles:
            raise ValueError(f"confined task binding parameter {param!r} collides with another argument")
        handles[param] = _ConfinedCarrierGitRepo(root=root, authority=authority, binding=binding)
    return handles


def _read_request(path: Path) -> Mapping[str, object]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise TypeError("confined task request must be an object")
    if raw.get("schema") != _REQUEST_SCHEMA:
        raise ValueError(f"unsupported confined task request schema: {raw.get('schema')!r}")
    return raw


def _write_error(exc: BaseException) -> None:
    sys.stderr.write(
        json.dumps(
            {
                "schema": _ERROR_SCHEMA,
                "status": "error",
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            }
        )
    )
    sys.stderr.write("\n")


def _resolve_qualname(module: Any, qualname: str) -> Any:
    value = module
    for part in qualname.split("."):
        if part == "<locals>":
            raise RuntimeError("task artifact entrypoint cannot reference a local function")
        value = getattr(value, part)
    # The executing artifact is always a plain function. When the module spells the
    # task with `@sp.task`, the exported attribute is the CallableTask wrapper, whose
    # __call__ expects the ambient nucleus workspace (unavailable in the confined
    # runner). Unwrap to the underlying function so the body runs directly.
    return inspect.unwrap(value)


def _portable(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, _ConfinedCarrierGitRepo):
        return {
            "kind": "shepherd.workspace_control.carrier_git_repo_result.v1",
            "binding": value.binding,
            "authority": value.authority,
        }
    if isinstance(value, tuple | list):
        return [_portable(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _portable(item) for key, item in value.items()}
    return {"kind": "python.repr", "type": type(value).__name__, "repr": repr(value)}


def _required_str(value: Mapping[str, object], field_name: str) -> str:
    raw = value.get(field_name)
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"confined task request field {field_name!r} must be a non-empty string")
    return raw


def _required_mapping(value: Mapping[str, object], field_name: str) -> Mapping[str, object]:
    raw = value.get(field_name)
    if not isinstance(raw, Mapping):
        raise TypeError(f"confined task request field {field_name!r} must be an object")
    return raw


def _optional_mapping(value: Mapping[str, object], field_name: str) -> Mapping[str, object]:
    raw = value.get(field_name)
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise TypeError(f"confined task request field {field_name!r} must be an object")
    return raw


def _validate_workspace_relative_path(path: str) -> None:
    if not isinstance(path, str):
        raise TypeError("workspace repo write path must be a relative POSIX path")
    parsed = PurePosixPath(path)
    if path in {"", ".", ".."} or parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise RuntimeError("workspace repo write path must be a relative POSIX path")


if __name__ == "__main__":
    raise SystemExit(main())
