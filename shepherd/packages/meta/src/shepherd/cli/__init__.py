"""Shepherd CLI — command-line tools for the Shepherd framework."""

from __future__ import annotations

import click
from shepherd_dialect.cli import run, task

from shepherd.cli.codex import codex
from shepherd.cli.demo import demo
from shepherd.cli.doctor import doctor
from shepherd.cli.init import init
from shepherd.cli.package import package


@click.group()
@click.version_option(package_name="shepherd")
def main() -> None:
    """Shepherd — effect-based AI agent framework."""


main.add_command(init)
main.add_command(package)
main.add_command(doctor)
main.add_command(demo)
main.add_command(codex)
main.add_command(run)
main.add_command(task)

__all__ = ["main"]
