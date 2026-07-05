"""Shepherd CLI — command-line tools for the Shepherd framework."""

from __future__ import annotations

import click
from shepherd_dialect import scoped_seal_and_select
from shepherd_dialect.cli import run, task

from shepherd.cli.demo import demo
from shepherd.cli.doctor import doctor
from shepherd.cli.init import init
from shepherd.cli.package import package


@click.group()
@click.version_option(package_name="shepherd")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Shepherd — effect-based AI agent framework."""
    # Scope the seal-and-select lane to THIS invocation. The previous
    # os.environ.setdefault mutated ambient process state that never got
    # restored, leaking across in-process CliRunner calls and flipping other
    # tests' readiness lane (W1c). with_resource exits the context when the
    # click context tears down at the end of command execution.
    ctx.with_resource(scoped_seal_and_select())


main.add_command(init)
main.add_command(package)
main.add_command(doctor)
main.add_command(demo)
main.add_command(run)
main.add_command(task)

__all__ = ["main"]
