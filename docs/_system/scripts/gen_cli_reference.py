#!/usr/bin/env python3
"""Generate docs/reference/cli.md from the checked CLI fixture (simulated
`shepherd --help` capture) — the second generate+drift pipeline. --check
fails on any difference between the committed page and a regeneration."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROTO = Path(__file__).resolve().parents[3] # repo root (content lives at repo root; tooling in docs_system/)
FIXTURE = PROTO / "docs/_src/shepherd/_sim/cli-help.json"
PAGE = PROTO / "docs/shepherd/reference/cli.md"

HEAD = """# CLI

> Page status: scaffold
> Source state: checked-fixture
> Applies to: Shepherd v0.1.1-dev
> Owner: @docs-system-owner (TBD)
> Validation: scripts/gen_cli_reference.py --check

*Reference. Exact, generated facts. The mental model lives in concepts, recipes in guides.*

!!! warning "CLI not shipped yet"
    The Shepherd CLI has not shipped. This page previews the planned command
    surface; the commands below are not runnable yet.

The command groups follow: first-run (`init`, `doctor`, `demo`),
`provider`, `placement`, `workflow`, and `run`/`runs`. Read-only listings
support `--json`.
"""


def render() -> str:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    parts = [HEAD]
    for cmd in data["commands"]:
        parts.append(f"\n## `{cmd['name']}`\n\n```text\n{cmd['help']}\n```\n")
    return "".join(parts)


def main() -> int:
    content = render()
    if "--check" in sys.argv:
        if not PAGE.exists() or PAGE.read_text(encoding="utf-8") != content:
            print("DRIFT: docs/reference/cli.md is stale vs the CLI fixture.")
            print("fix:./check.sh regen (see docs/_runbook.md)")
            return 1
        print("ok: cli.md matches the fixture")
        return 0
    PAGE.write_text(content, encoding="utf-8", newline="\n")
    print(f"wrote {PAGE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
