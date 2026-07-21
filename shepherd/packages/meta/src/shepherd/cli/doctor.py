"""Quickstart readiness checks for the Shepherd CLI."""

from __future__ import annotations

import json
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any, Literal

import click

DoctorMode = Literal["core", "claude", "codex", "hermes"]


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
        # Group-level options would be parsed and silently dropped here — a CI
        # script writing `doctor --json hermes` would pipe human output to jq.
        # Refuse loudly instead of half-honoring the invocation.
        if json_output or backend != "auto":
            raise click.UsageError(
                f"place --json/--backend after the subcommand, e.g. `shepherd doctor {ctx.invoked_subcommand} --json`"
            )
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
    help="Check Claude CLI auth under Shepherd's scrubbed config/seeding conditions "
    "(reaches the network; may briefly call the model). Not a jailed run.",
)
def doctor_claude(json_output: bool, backend: str, probe: bool) -> None:
    """Check whether the live Claude runtime lane is available."""
    _run_doctor(mode="claude", json_output=json_output, backend=backend, probe=probe)


@doctor.command("codex")
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable readiness JSON.")
@click.option(
    "--backend",
    type=click.Choice(["auto", "clonefile", "fuse", "kernel", "copy"]),
    default="auto",
    show_default=True,
    help="Workspace backend to validate.",
)
@click.option("--profile", default="default", show_default=True, help="Shepherd Codex authentication profile.")
@click.option(
    "--probe",
    is_flag=True,
    help="Refresh and read the ChatGPT account through app-server (networked, but does not call a model).",
)
def doctor_codex(json_output: bool, backend: str, profile: str, probe: bool) -> None:
    """Check whether the headless Codex lane is available."""
    _run_doctor(mode="codex", json_output=json_output, backend=backend, probe=probe, profile=profile)


@doctor.command("hermes")
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable readiness JSON.")
@click.option(
    "--backend",
    type=click.Choice(["auto", "clonefile", "fuse", "kernel", "copy"]),
    default="auto",
    show_default=True,
    help="Workspace backend to validate.",
)
@click.option(
    "--model",
    default=None,
    help="Model id for the probe — the hermes lane has no account default.",
)
@click.option(
    "--provider",
    "model_provider",
    default=None,
    help="Model provider: openai-codex (a ChatGPT subscription), anthropic, openai, or openrouter.",
)
@click.option(
    "--probe",
    is_flag=True,
    help="Check hermes auth under Shepherd's scrubbed-home + seeded-config conditions "
    "(reaches the network; briefly calls the model). Not a jailed run. Needs --model and --provider.",
)
def doctor_hermes(json_output: bool, backend: str, model: str | None, model_provider: str | None, probe: bool) -> None:
    """Check whether the hermes multi-model runtime lane is available."""
    _run_doctor(
        mode="hermes", json_output=json_output, backend=backend, probe=probe, model=model, model_provider=model_provider
    )


def _run_doctor(
    *,
    mode: DoctorMode,
    json_output: bool,
    backend: str,
    probe: bool = False,
    model: str | None = None,
    model_provider: str | None = None,
    profile: str = "default",
) -> None:
    checks = _core_checks(backend=backend)
    if mode == "claude":
        checks.extend(_claude_checks(probe=probe))
    elif mode == "codex":
        checks.extend(_codex_checks(profile=profile, probe=probe))
    elif mode == "hermes":
        checks.extend(_hermes_checks(model=model, model_provider=model_provider, probe=probe))

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


def _codex_checks(*, profile: str, probe: bool = False) -> list[dict[str, object]]:
    checks: list[dict[str, object]] = [_native_jail_check()]
    try:
        from shepherd_dialect import CODEX_TESTED_VERSION, codex_auth_status

        status = codex_auth_status(profile)
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("codex-sdk", False, f"could not inspect Codex dependency: {exc}"))
        checks.append(_check("codex-auth", False, f"could not inspect profile: {exc}"))
        return checks
    checks.append(
        _check(
            "codex-sdk",
            status.runtime_compatible,
            status.sdk_version or f"not installed (tested {CODEX_TESTED_VERSION})",
        )
    )
    checks.append(_check("codex-auth", status.ok, status.detail))
    if probe:
        try:
            from shepherd_dialect import probe_codex_auth

            ok, detail = probe_codex_auth(profile)
        except Exception as exc:  # noqa: BLE001
            checks.append(_check("codex-auth-probe", False, f"probe error: {exc}"))
        else:
            checks.append(_check("codex-auth-probe", ok, detail))
    return checks


def _claude_checks(*, probe: bool = False) -> list[dict[str, object]]:
    checks: list[dict[str, object]] = [_native_jail_check()]

    claude_path = shutil.which("claude")
    checks.append(_check("claude-cli", claude_path is not None, claude_path or "`claude` not found on PATH"))

    # Offline: honest about what is *knowable* without a round-trip. A readable
    # but expired subscription blob is a hard fail — a jailed run cannot refresh
    # it — so a green `claude-auth` predicts a working run rather than "a blob
    # exists". `--probe` is the network-reaching confirmation under Shepherd's
    # scrubbed-config conditions (in the parent, not through the jail).
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


def _native_jail_check() -> dict[str, object]:
    """The jail readiness check both agent lanes share."""
    try:
        from shepherd_dialect import native_jail_available

        jail_ok = native_jail_available()
    except Exception as exc:  # noqa: BLE001
        return _check("native-jail", False, f"could not check native jail: {exc}")
    return _check("native-jail", jail_ok, "available" if jail_ok else "unavailable")


def _hermes_checks(*, model: str | None, model_provider: str | None, probe: bool = False) -> list[dict[str, object]]:
    # Normalize at the boundary the way the workspace router does
    # (strip + lower): a user typing `--provider Anthropic` meant anthropic,
    # and "unsupported model_provider 'Anthropic'" would be a lie.
    model = model.strip() if model else None
    model_provider = model_provider.strip().lower() if model_provider else None
    checks: list[dict[str, object]] = [_native_jail_check()]

    hermes_path = shutil.which("hermes")
    checks.append(_check("hermes-cli", hermes_path is not None, hermes_path or "`hermes` not found on PATH"))
    if hermes_path is not None:
        checks.append(_hermes_version_check(hermes_path))

    # The hermes lane has no account default: model selection and auth routing
    # are explicit (execplan 260709 r5 §S2), so missing arguments are red
    # checks naming the flag, not crashes or silent fallbacks.
    try:
        from shepherd_dialect import HERMES_SUPPORTED_MODEL_PROVIDERS, hermes_auth_status

        if not model_provider:
            supported = ", ".join(HERMES_SUPPORTED_MODEL_PROVIDERS)
            checks.append(
                _check("hermes-auth", False, f"pass --provider ({supported}) — the hermes lane has no account default")
            )
        else:
            status = hermes_auth_status(model_provider)
            checks.append(_check("hermes-auth", status.ok, status.detail))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("hermes-auth", False, f"could not check hermes auth: {exc}"))

    if probe:
        if hermes_path is None:
            checks.append(_check("hermes-auth-probe", False, "skipped: `hermes` not on PATH"))
        elif not model or not model_provider:
            missing = " and ".join(
                flag for flag, value in (("--model", model), ("--provider", model_provider)) if not value
            )
            checks.append(
                _check("hermes-auth-probe", False, f"skipped: the probe needs {missing} (no account default)")
            )
        else:
            try:
                from shepherd_dialect import probe_hermes_auth

                probe_ok, probe_detail = probe_hermes_auth(model=model, model_provider=model_provider)
            except Exception as exc:  # noqa: BLE001
                checks.append(_check("hermes-auth-probe", False, f"probe error: {exc}"))
            else:
                checks.append(_check("hermes-auth-probe", probe_ok, probe_detail))
    return checks


def _hermes_version_check(cli_path: str) -> dict[str, object]:
    """The warn-only version pin: optional, so a drifted lane never gates readiness.

    The tested-version baseline is provider knowledge — imported from the
    dialect package, which owns the spiked-version claim, so a bump has one
    home (r5 review).
    """
    try:
        from shepherd_dialect import HERMES_REAUDIT_ON_BUMP, HERMES_TESTED_VERSION
    except Exception as exc:  # noqa: BLE001
        return _check("hermes-version", False, f"could not read the tested-version baseline: {exc}", required=False)
    version = _hermes_version(cli_path)
    if version is None:
        return _check(
            "hermes-version", False, "could not determine offline (package metadata unavailable)", required=False
        )
    if version == HERMES_TESTED_VERSION:
        return _check("hermes-version", True, f"{version} (tested)", required=False)
    return _check(
        "hermes-version",
        False,
        f"{version} differs from the tested {HERMES_TESTED_VERSION} — re-audit {HERMES_REAUDIT_ON_BUMP} on the bump",
        required=False,
    )


def _hermes_version(cli_path: str) -> str | None:
    """The installed hermes-agent version *for the CLI on PATH*, resolved offline. Never raises.

    This interpreter's package metadata is trusted only when ``cli_path``
    actually lives under this interpreter's prefix — otherwise a hermes-agent
    installed in shepherd's own venv would green-stamp a *different* PATH
    install, losing the drift warning exactly when it matters (r5 review).
    Separate-venv installs resolve through the CLI script's shebang
    interpreter — metadata reads only. ``hermes --version`` is deliberately
    avoided: it fires the upstream update check, a network call an offline
    doctor must not make.
    """
    try:
        if Path(cli_path).resolve().is_relative_to(Path(sys.prefix).resolve()):
            from importlib.metadata import version

            return version("hermes-agent")
    except Exception:  # noqa: BLE001, S110 — fall through to the shebang interpreter
        pass
    try:
        shebang = Path(cli_path).read_text(encoding="utf-8", errors="ignore").splitlines()[0]
        if not shebang.startswith("#!"):
            return None
        # `#!/usr/bin/env python3` and flag-carrying shebangs are token lists,
        # not a single path; and only a python interpreter can run the
        # metadata one-liner (pip's sh-wrapper launchers cannot).
        tokens = shlex.split(shebang[2:])
        if tokens and Path(tokens[0]).name == "env":
            tokens = tokens[1:]
        if not tokens or not Path(tokens[0]).name.startswith("python"):
            return None
        import subprocess

        proc = subprocess.run(
            [*tokens, "-c", "from importlib.metadata import version; print(version('hermes-agent'))"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return proc.stdout.strip() or None
    except Exception:  # noqa: BLE001 — an undeterminable version is a warn, not a crash
        return None


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
