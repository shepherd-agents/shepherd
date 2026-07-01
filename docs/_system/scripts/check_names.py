#!/usr/bin/env python3
"""Stale-name gate (DESIGN 1.8): no internal package names (`agentic*`, `shepherd_*`) or `device` leak on public pages.

Public pages speak the `shepherd` facade; the bare `shepherd`/`Shepherd` brand and
dotted `shepherd.<symbol>` paths are allowed. Internal implementation package names
(`shepherd_runtime`, `shepherd_dialect`, …), any residual pre-rename `agentic*`, and
`device` must not appear.

Relief valves: the `> Stale-names: migration-context` marker (page-wide; the
page must be enumerated in the allowlist) and allowlist path patterns. Exit
0/1/2; ASCII output.
"""

from __future__ import annotations

import re
from pathlib import Path

from check_shepherd_docs import load_config, parse_meta  # tolerant-but-inert loader (see its docstring)
from pathspec.gitignore import GitIgnoreSpec

PROTO = Path(__file__).resolve().parent.parent
# Leading \b so internal package names (`shepherd_runtime`, …) are caught but the
# validator script names cited in metadata (`check_shepherd_docs.py`,
# `gen_shepherd_ref_pages.py`) are NOT — the `_` before `shepherd` is no word boundary.
PATTERN = re.compile(r"\bagentic\w*|\bshepherd_\w+|\bdevice\b", re.IGNORECASE)


def main() -> int:
    cfg = load_config(PROTO / "mkdocs.yml")
    docs = PROTO / cfg.get("docs_dir", "docs")
    spec = GitIgnoreSpec.from_lines([ln for ln in (cfg.get("exclude_docs") or "").splitlines() if ln.strip()])

    allow_path = docs / "_expected-forward.txt"
    allow: list[str] = []
    if allow_path.exists():
        for ln in allow_path.read_text(encoding="utf-8").splitlines():
            ln = ln.split("#", 1)[0].strip()
            if ln:
                allow.append(ln)
    allow_spec = GitIgnoreSpec.from_lines(allow) if allow else None

    errors = []
    for md in sorted(docs.rglob("*.md")):
        rel = md.relative_to(docs).as_posix()
        if rel.startswith("_templates/") or rel in ("_runbook.md",) or spec.match_file(rel):
            continue  # not in the public build
        text = md.read_text(encoding="utf-8")
        marked = "Stale-names: migration-context" in parse_meta(text).get("Stale-names", "") or \
                 re.search(r"^> Stale-names: migration-context", text, re.MULTILINE)
        enumerated = bool(allow_spec and allow_spec.match_file(rel))
        if marked and not enumerated:
            errors.append(f"[NAMES] {rel}: carries migration-context marker but is not enumerated in _expected-forward.txt")
            continue
        if marked and enumerated:
            continue
        if enumerated:
            continue
        hits = sorted({m.group(0) for m in PATTERN.finditer(text)})
        if hits:
            errors.append(f"[NAMES] {rel}: stale name(s) on public page: {', '.join(hits[:6])}  (fix or S12 allowlist; see docs/_runbook.md)")

    for e in errors:
        print("ERROR " + e)
    print(f"\nnames: errors={len(errors)}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
