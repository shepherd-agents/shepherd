"""Codex subscription-profile lifecycle commands."""

from __future__ import annotations

from pathlib import Path

import click


@click.group("codex")
def codex() -> None:
    """Manage headless Codex authentication profiles."""


@codex.command("login")
@click.option("--profile", default="default", show_default=True)
@click.option("--mode", type=click.Choice(["chatgpt", "api-key"]), default="chatgpt", show_default=True)
def login(profile: str, mode: str) -> None:
    """Sign a profile in with ChatGPT or a privately prompted API key."""
    from shepherd_dialect import login_codex_api_key, login_codex_chatgpt

    if mode == "api-key":
        api_key = click.prompt("OpenAI API key", hide_input=True, confirmation_prompt=False)
        login_codex_api_key(profile, api_key)
        click.echo(f"Codex profile {profile!r} is configured for API-key auth.")
        return

    def show_code(url: str, user_code: str) -> None:
        click.echo(f"Open {url}")
        click.echo(f"Enter code: {user_code}")
        click.echo("Waiting for sign-in…")

    login_codex_chatgpt(profile, on_device_code=show_code)
    click.echo(f"Codex profile {profile!r} is signed in with ChatGPT.")


@codex.command("adopt")
@click.option("--profile", default="default", show_default=True)
@click.option("--source-home", type=click.Path(path_type=Path), default=None)
def adopt(profile: str, source_home: Path | None) -> None:
    """Explicitly link an existing Codex login without copying token bytes."""
    from shepherd_dialect import adopt_existing_codex_login

    adopt_existing_codex_login(profile, source_home=source_home)
    click.echo(f"Codex profile {profile!r} now uses the explicitly selected existing login.")


@codex.command("status")
@click.option("--profile", default="default", show_default=True)
@click.option("--probe", is_flag=True, help="Refresh/read account state without calling a model.")
def status(profile: str, probe: bool) -> None:
    """Show non-secret profile and compatibility readiness."""
    from shepherd_dialect import codex_auth_status, probe_codex_auth

    current = codex_auth_status(profile)
    click.echo(current.detail)
    if not current.ok:
        raise SystemExit(1)
    if probe:
        ok, detail = probe_codex_auth(profile)
        click.echo(detail)
        if not ok:
            raise SystemExit(1)


@codex.command("logout")
@click.option("--profile", default="default", show_default=True)
def logout(profile: str) -> None:
    """Remove only the selected Shepherd profile."""
    from shepherd_dialect import logout_codex_profile

    logout_codex_profile(profile)
    click.echo(f"Removed Codex profile {profile!r}.")


__all__ = ["codex"]
