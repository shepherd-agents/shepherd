"""Quickstart readiness checks for the Shepherd CLI."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any, Literal

import click

DoctorMode = Literal["core", "claude"]


@click.group(invoke_without_command=True)
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable readiness JSON.")
@click.option(
    "--backend",
    type=click.Choice(["auto", "clonefile", "fuse", "kernel", "copy"]),
    default="auto",
    show_default=True,
    help="Workspace backend to validate.",
)
@click.pass_context
def doctor(ctx: click.Context, json_output: bool, backend: str) -> None:
    """Check whether the current directory is ready for the quickstart."""
    if ctx.invoked_subcommand is not None:
        return
    _run_doctor(mode="core", json_output=json_output, backend=backend)


@doctor.command("claude")
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable readiness JSON.")
@click.option(
    "--backend",
    type=click.Choice(["auto", "clonefile", "fuse", "kernel", "copy"]),
    default="auto",
    show_default=True,
    help="Workspace backend to validate.",
)
@click.option(
    "--probe",
    is_flag=True,
    help="Authenticate the jailed Claude lane for real (reaches the network; may briefly call the model).",
)
def doctor_claude(json_output: bool, backend: str, probe: bool) -> None:
    """Check whether the live Claude runtime lane is available."""
    _run_doctor(mode="claude", json_output=json_output, backend=backend, probe=probe)


def _run_doctor(*, mode: DoctorMode, json_output: bool, backend: str, probe: bool = False) -> None:
    checks = _core_checks(backend=backend)
    if mode == "claude":
        checks.extend(_claude_checks(probe=probe))

    payload = {"mode": mode, "checks": checks, "ok": all(check["ok"] or not check["required"] for check in checks)}
    if json_output:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _emit_human(payload)
    if not payload["ok"]:
        raise SystemExit(1)


def _core_checks(*, backend: str) -> list[dict[str, object]]:
    cwd = Path.cwd()
    checks: list[dict[str, object]] = [
        _check(
            "python",
            sys.version_info >= (3, 11),
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        ),
        _check("cwd", cwd.is_dir(), str(cwd)),
        _check("git", _git_root(cwd) is not None, _git_root(cwd) or "not inside a Git repository"),
    ]

    vcscore = cwd / ".vcscore"
    checks.append(_check("vcscore", vcscore.exists(), str(vcscore) if vcscore.exists() else "run `sp init`"))
    if vcscore.exists():
        checks.append(_workspace_activation_check(cwd, backend=backend))
    else:
        checks.append(_check("workspace", False, "not initialized", required=True))
    return checks


def _claude_checks(*, probe: bool = False) -> list[dict[str, object]]:
    checks: list[dict[str, object]] = []
    try:
        from shepherd_dialect import native_jail_available

        jail_ok = native_jail_available()
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("native-jail", False, f"could not check native jail: {exc}"))
    else:
        checks.append(_check("native-jail", jail_ok, "available" if jail_ok else "unavailable"))

    claude_path = shutil.which("claude")
    checks.append(_check("claude-cli", claude_path is not None, claude_path or "`claude` not found on PATH"))

    # Offline: honest about what is *knowable* without a round-trip. A readable
    # but expired subscription blob is a hard fail — a jailed run cannot refresh
    # it — so a green `claude-auth` predicts a working run rather than "a blob
    # exists". `--probe` is the authoritative, network-reaching confirmation.
    try:
        from shepherd_dialect import claude_auth_status

        status = claude_auth_status()
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("claude-auth", False, f"could not check Claude auth: {exc}"))
    else:
        checks.append(_check("claude-auth", status.ok, status.detail))

    if probe:
        if claude_path is None:
            checks.append(_check("claude-auth-probe", False, "skipped: `claude` not on PATH"))
        else:
            try:
                from shepherd_dialect import probe_claude_auth

                probe_ok, probe_detail = probe_claude_auth()
            except Exception as exc:  # noqa: BLE001
                checks.append(_check("claude-auth-probe", False, f"probe error: {exc}"))
            else:
                checks.append(_check("claude-auth-probe", probe_ok, probe_detail))
    return checks


def _workspace_activation_check(cwd: Path, *, backend: str) -> dict[str, object]:
    try:
        from shepherd_dialect.workspace_control import ShepherdWorkspace

        selected_backend = None if backend == "auto" else backend
        workspace = ShepherdWorkspace.discover(cwd, activate=True, backend=selected_backend)
    except Exception as exc:  # noqa: BLE001
        return _check("workspace", False, f"activation failed: {exc}", required=True)
    else:
        workspace.close()
        return _check("workspace", True, f"activated with backend={backend}", required=True)


def _git_root(cwd: Path) -> str | None:
    import subprocess

    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _check(name: str, ok: bool, detail: object, *, required: bool = True) -> dict[str, object]:
    return {"name": name, "ok": bool(ok), "detail": detail, "required": required}


def _emit_human(payload: dict[str, Any]) -> None:
    click.echo(f"Shepherd doctor ({payload['mode']})")
    for check in payload["checks"]:
        mark = "ok" if check["ok"] else "fail"
        required = "" if check["required"] else " (optional)"
        click.echo(f"  {mark:4s} {check['name']}{required}: {check['detail']}")
    if payload["ok"]:
        click.echo("\nReady.")
    else:
        click.echo("\nNot ready.")


__all__ = ["doctor"]
