#!/usr/bin/env python3
"""Cross-platform entry point for the Shepherd docs pipeline (DESIGN 1.10).

ONE source of truth for the pipeline: the numbered.sh wrappers in this folder
are thin shims that just call `uv run python run.py <cmd>`. Runs on Linux/WSL2
and macOS; the only tool a user needs on PATH is `uv`.

Every step shells out to:

    uv run --no-project --with-requirements docs-requirements.txt <tool>...

so deps are pinned (reproducible — FINDINGS: they used to float latest) while
keeping `--no-project` (this folder is not a uv project). This script resolves
its own directory and sets the required env vars in-process, so it behaves the
same whatever the caller's cwd or shell.

Subcommands:
  check [--regen] drift -> gates -> example tests -> strict builds -> assert
  regen alias for `check --regen`
  preview live PUBLIC site (default-deny) http://127.0.0.1:8000
  preview-internal live REVIEWER site (full tree) http://127.0.0.1:8001
  promote <page> promote a page (un-exclude + nav + gate); see scripts/promote.py
  pages [filter] show the page map: producer + status + public + backing
  deploy check, then publish the public site to GitHub Pages (gh-deploy)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REQUIREMENTS = "docs-requirements.txt"

# Resolved deps for every Python/mkdocs/pytest/ruff invocation. Pinned via the
# compiled lock; --no-project because this prototype folder is not a uv project.
UV_BASE = [
    "uv", "run", "--no-project",
    "--with-requirements", str(HERE / REQUIREMENTS),
]

# UTF-8 is mandatory: the content carries em-dashes and emoji; without
# PYTHONUTF8=1 a Windows cp1252 default crashes mkdocs/pytest. The mkdocs-2
# pre-release nag is silenced with NO_MKDOCS_2_WARNING (DISABLE_* does nothing).
ENV = {**os.environ, "PYTHONUTF8": "1", "NO_MKDOCS_2_WARNING": "true"}


def say(msg: str = "") -> None:
    # flush so our headers stay ordered relative to subprocess output even when
    # this script's stdout is block-buffered through a pipe.
    print(msg, flush=True)


def uv(*args: str) -> None:
    """Run a tool inside the pinned uv environment; abort the pipeline on failure."""
    cmd = [*UV_BASE, *args]
    rc = subprocess.call(cmd, cwd=str(HERE), env=ENV)
    if rc != 0:
        sys.exit(rc)


def py(script: str, *args: str) -> None:
    uv("python", f"scripts/{script}", *args)


def regen() -> None:
    say("== regen: API pages + snapshot + CLI page ==")
    py("gen_shepherd_ref_pages.py")
    py("gen_shepherd_api_inventory.py")
    py("gen_cli_reference.py")


def check(do_regen: bool) -> None:
    if do_regen:
        regen()

    say("== drift checks (generated artifacts vs sources) ==")
    py("gen_shepherd_ref_pages.py", "--check")
    py("gen_shepherd_api_inventory.py", "--check")
    py("gen_cli_reference.py", "--check")

    say("== membership gate ==")
    py("check_shepherd_docs.py")

    say("== stale-name gate ==")
    py("check_names.py")

    say("== workflow-fixture check ==")
    py("check_shepherd_docs_workflows.py")

    say("== documented examples (simulated offline provider) ==")
    uv("pytest", "../_src/shepherd", "-q")

    say("== strict builds: public (default-deny) + internal (full) ==")
    uv("mkdocs", "build", "--strict", "-f", "mkdocs.yml")
    uv("mkdocs", "build", "--strict", "-f", "mkdocs.internal.yml")
    (HERE / "site/internal/INTERNAL_BUILD_DO_NOT_DEPLOY").write_text(
        "INTERNAL REVIEWER BUILD - DO NOT DEPLOY TO A PUBLIC ORIGIN\n",
        encoding="utf-8", newline="\n",
    )

    link_audit()

    say("== built-output assertion (page set + styled site) ==")
    py("check_shepherd_docs.py", "--assert-built", "site/shepherd")

    say()
    say("ALL GREEN. Preview: run.py preview (public,:8000) | "
        "run.py preview-internal (full,:8001)")


def preview(internal: bool) -> None:
    # mkdocs serve blocks forever (Ctrl-C to stop) — that is intentional here.
    cfg = "mkdocs.internal.yml" if internal else "mkdocs.yml"
    addr = "127.0.0.1:8001" if internal else "127.0.0.1:8000"
    which = "INTERNAL reviewer" if internal else "PUBLIC (default-deny)"
    say(f"Serving the {which} site at http://{addr} (Ctrl-C to stop)")
    uv("mkdocs", "serve", "-f", cfg, "-a", addr)


def promote(page: str, dry_run: bool) -> None:
    args = ["scripts/promote.py", page]
    if dry_run:
        args.append("--dry-run")
    uv("python", *args)


def pages(filt: str) -> None:
    py("pages.py", *([filt] if filt else []))


def deploy() -> None:
    # Never deploy a red build: run the full gate first, then publish to GitHub Pages.
    check(do_regen=False)
    say("== deploy: mkdocs gh-deploy (public build -> gh-pages) ==")
    uv("mkdocs", "gh-deploy", "-f", "mkdocs.yml")
    say()
    say("DEPLOYED. The public site was pushed to the gh-pages branch.")


def link_audit() -> None:
    """link check via the repo's docs/tools/link-audit.py; skips cleanly if
    the tool is absent so the pipeline still runs anywhere."""
    tool = HERE.parent.parent / "docs/tools/link-audit.py"
    if not tool.exists():
        say("== link audit: skipped (docs/tools/link-audit.py not found) ==")
        return
    say("== link audit (docs/shepherd) ==")
    uv("python", str(tool), "../shepherd")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="run.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="run the full pipeline")
    c.add_argument("--regen", action="store_true",
                   help="regenerate API/CLI artifacts first (after facade/fixture/_map changes)")

    sub.add_parser("regen", help="alias for `check --regen`")
    sub.add_parser("preview", help="live PUBLIC site at:8000 (blocks)")
    sub.add_parser("preview-internal", help="live REVIEWER site at:8001 (blocks)")

    p = sub.add_parser("promote", help="promote a page (un-exclude + nav + gate)")
    p.add_argument("page", help="page path relative to docs/, e.g. concepts/effects.md")
    p.add_argument("--dry-run", action="store_true", help="show planned edits, write nothing")

    pg = sub.add_parser("pages", help="show the page map (producer/status/public/backing)")
    pg.add_argument("filter", nargs="?", default="", help="only pages whose path contains this")

    sub.add_parser("deploy", help="check, then publish the public site to GitHub Pages (gh-deploy)")

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "check":
        check(args.regen)
    elif args.cmd == "regen":
        check(do_regen=True)
    elif args.cmd == "preview":
        preview(internal=False)
    elif args.cmd == "preview-internal":
        preview(internal=True)
    elif args.cmd == "promote":
        promote(args.page, args.dry_run)
    elif args.cmd == "pages":
        pages(args.filter)
    elif args.cmd == "deploy":
        deploy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
