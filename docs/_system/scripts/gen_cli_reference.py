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

> Page status: release-ready
> Source state: shipped-source
> Applies to: Shepherd v0.3.0
> Owner: @docs-system-owner (TBD)
> Validation: scripts/gen_cli_reference.py --check

*Reference. Exact, generated facts. The mental model lives in concepts, recipes in guides.*

The `shepherd` command (also installed as `sp`) ships in 0.3.0. The help blocks
below are captured verbatim from the shipped CLI. Read-only listings accept
`--json` for a durable machine payload.

**Read vs. settle — the identity rule.** Read commands (`show`, `changeset`,
`trace`, …) accept selectors: `--latest` and a unique short run-id prefix.
Settlement commands (`select` / `apply` / `release` / `discard`) require an
*exact* run identity and reject selectors. Settlement is consume-once: after one
records its outcome, the others refuse for that output.

`run start` is a fenced compatibility entry point, not the normal launch path —
it fails closed unless `SHEPHERD_ENABLE_FENCED_RUN_START=1` is set. The sanctioned
Python launch is `workspace.run(...)`.
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
