#!/usr/bin/env python3
"""Promote a page to the public (default-deny) build (DESIGN §4 S4).

Kills the manual 2-file edit. Promotion = (1) the page carries the metadata
triple + modeline, (2) it is un-excluded in mkdocs.yml exclude_docs, (3) it is
in the public nav. This helper validates (1) reusing the membership gate's
logic, then performs (2) and (3) by SURGICAL TEXT INSERTION — it never dumps
YAML over mkdocs.yml, so the hand-written comments and the `!!python/name:`
emoji tags survive byte-for-byte. After editing it re-runs the gate; if the
gate is not green it REVERTS the edit and reports, so a bad promote can never
leave mkdocs.yml corrupted or the build red.

Usage:
  promote.py <page-rel-path>     e.g.  promote.py concepts/effects.md
  promote.py <page> --dry-run    show the planned edits, write nothing

Exit 0 promoted / 1 not-ready or gate-failed-and-reverted / 2 usage.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Reuse the gate so "promotion-ready" means exactly what the gate enforces.
from check_shepherd_docs import (
    ADMISSIBLE,
    MODE_LINES,
    PROTO,
    page_type,
    parse_meta,
)
from check_shepherd_docs import (
    main as gate_main,
)

CONFIG = PROTO / "mkdocs.yml"
DOCS = PROTO.parent.parent / "docs/shepherd"  # content at repo root; PROTO (docs_system/) holds mkdocs.yml

# Top-level docs dir -> public nav section title. Mirrors the gate's TYPE_BY_DIR
# keys; titles match the existing sections in mkdocs.yml's nav.
DIR_TO_SECTION = {
    "start": "Start",
    "tutorials": "Tutorials",
    "concepts": "Concepts",
    "reference": "Reference",
    "workflows": "Workflows",
    "guides": "Guides",
}


# --------------------------------------------------------------------------- #
# readiness (mirrors the gate's nav-admissibility + modeline rules)
# --------------------------------------------------------------------------- #
def readiness_problems(rel: str, text: str) -> list[str]:
    meta = parse_meta(text)
    problems: list[str] = []

    status = meta.get("Page status")
    if status != "release-ready":
        problems.append(f"Page status is {status!r}; promotion needs 'release-ready'")

    state = meta.get("Source state")
    if state not in ADMISSIBLE:
        problems.append(
            f"Source state is {state!r}; promotion needs one of {sorted(ADMISSIBLE)}"
        )

    validation = meta.get("Validation", "")
    if validation.strip().lower() in ("", "not yet validated"):
        problems.append("Validation is empty / 'not yet validated'; record how the page is checked")

    ptype = page_type(rel)
    if ptype and MODE_LINES[ptype] not in text:
        problems.append(f"missing the canonical {ptype} mode line (copy it from the matching page-mode template)")
    if ptype == "concept" and "## Going deeper" not in text:
        problems.append("concept page lacks a '## Going deeper' footer (C4)")

    return problems


# --------------------------------------------------------------------------- #
# nav label
# --------------------------------------------------------------------------- #
def nav_entry(rel: str, text: str) -> str:
    """The nav line under the section. Index pages get a bare path (matching the
    existing `- workflows/index.md`); other pages get `Title: path` from the H1."""
    if Path(rel).name == "index.md":
        return f"      - {rel}"
    title = None
    for line in text.splitlines():
        m = re.match(r"^#\s+(.+?)\s*$", line)
        if m:
            title = m.group(1).strip().strip("`")
            break
    if not title:
        title = Path(rel).stem.replace("-", " ").replace("_", " ").capitalize()
    return f"      - {title}: {rel}"


# --------------------------------------------------------------------------- #
# surgical edits to mkdocs.yml text
# --------------------------------------------------------------------------- #
def add_unexclusion(cfg_text: str, rel: str) -> tuple[str, str]:
    """Insert `  !/<rel>` into exclude_docs after the last existing `  !/...`
    page line (before the !stylesheets/** , !assets/** asset lines)."""
    line = f"  !/{rel}"
    if re.search(rf"(?m)^{re.escape(line)}\s*$", cfg_text):
        return cfg_text, f"(already present) {line.strip()}"

    lines = cfg_text.splitlines(keepends=True)
    # Locate the exclude_docs block: the `exclude_docs: |` header, then its
    # indented body (lines starting with two spaces) until dedent.
    start = next(
        (i for i, ln in enumerate(lines) if re.match(r"^exclude_docs:\s*\|\s*$", ln)),
        None,
    )
    if start is None:
        raise RuntimeError("could not find an 'exclude_docs: |' block in mkdocs.yml")

    body_end = start + 1
    last_page = None  # index of the last `  !/...md` page un-exclusion
    while body_end < len(lines) and (lines[body_end].startswith("  ") or lines[body_end].strip() == ""):
        if re.match(r"^  !/.+", lines[body_end]):
            last_page = body_end
        body_end += 1

    insert_at = (last_page + 1) if last_page is not None else (start + 1)
    lines.insert(insert_at, line + "\n")
    return "".join(lines), line.strip()


def add_nav_entry(cfg_text: str, rel: str, entry_line: str) -> tuple[str, str]:
    """Insert the nav entry under the section matching the page's top-level dir,
    creating the section if absent."""
    top = rel.split("/", 1)[0]
    section = DIR_TO_SECTION.get(top, top.capitalize())

    if re.search(rf"(?m)^\s*-\s+.*:\s*{re.escape(rel)}\s*$", cfg_text) or \
       re.search(rf"(?m)^\s*-\s+{re.escape(rel)}\s*$", cfg_text):
        return cfg_text, f"(already in nav) {entry_line.strip()}"

    lines = cfg_text.splitlines(keepends=True)
    nav_start = next((i for i, ln in enumerate(lines) if re.match(r"^nav:\s*$", ln)), None)
    if nav_start is None:
        raise RuntimeError("could not find a 'nav:' block in mkdocs.yml")

    # Section headers look like `  - Concepts:` (2-space indent, no value).
    sec_re = re.compile(rf"^  - {re.escape(section)}:\s*$")
    sec_idx = None
    nav_end = nav_start + 1
    while nav_end < len(lines) and (lines[nav_end].startswith(" ") or lines[nav_end].strip() == ""):
        if sec_re.match(lines[nav_end]):
            sec_idx = nav_end
        nav_end += 1

    if sec_idx is not None:
        # Append after the section's existing children (4+-space indented lines).
        insert_at = sec_idx + 1
        while insert_at < len(lines) and lines[insert_at].startswith("      "):
            insert_at += 1
        lines.insert(insert_at, entry_line + "\n")
        how = f"under existing section '{section}'"
    else:
        # Create the section at the end of the nav block.
        block = f"  - {section}:\n{entry_line}\n"
        # Trim trailing blank lines inside the nav block before appending.
        while nav_end > nav_start + 1 and lines[nav_end - 1].strip() == "":
            nav_end -= 1
        lines.insert(nav_end, block)
        how = f"new section '{section}'"
    return "".join(lines), f"{entry_line.strip()}  [{how}]"


# --------------------------------------------------------------------------- #
def run_gate() -> int:
    """Invoke the membership gate's main() with a clean argv (it argparses
    sys.argv; promote's own args must not leak into it)."""
    saved = sys.argv
    sys.argv = ["check_shepherd_docs.py"]
    try:
        return gate_main()
    finally:
        sys.argv = saved


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(prog="promote.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("page", help="page path relative to docs/, e.g. concepts/effects.md")
    ap.add_argument("--dry-run", action="store_true", help="show planned edits, write nothing")
    args = ap.parse_args()

    rel = args.page.replace("\\", "/").strip()
    if rel.startswith("docs/"):
        rel = rel[len("docs/"):]
    page_path = DOCS / rel
    if not page_path.is_file():
        print(f"ERROR: no such page: docs/{rel}")
        return 2

    text = page_path.read_text(encoding="utf-8")

    problems = readiness_problems(rel, text)
    if problems:
        print(f"NOT PROMOTABLE: docs/{rel} is not promotion-ready. Fix, then re-run:")
        for p in problems:
            print(f"  - {p}")
        print("(see docs/_runbook.md S4)")
        return 1

    original = CONFIG.read_text(encoding="utf-8")
    try:
        updated, excl_msg = add_unexclusion(original, rel)
        updated, nav_msg = add_nav_entry(updated, rel, nav_entry(rel, text))
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1

    print(f"promote docs/{rel}")
    print(f"  exclude_docs: + {excl_msg}")
    print(f"  nav:          + {nav_msg}")

    if updated == original:
        print("nothing to do: already un-excluded and in nav.")
        return 0

    if args.dry_run:
        print("\n--dry-run: no files written. Run without --dry-run to apply.")
        return 0

    # Apply, then gate. If the gate is not green, REVERT so a bad promote never
    # leaves mkdocs.yml corrupted or the public build red.
    CONFIG.write_text(updated, encoding="utf-8", newline="\n")
    print("\n== membership gate (post-promote) ==")
    rc = run_gate()
    if rc != 0:
        CONFIG.write_text(original, encoding="utf-8", newline="\n")
        print("\nGATE FAILED -> reverted mkdocs.yml. No changes kept.")
        return 1

    print(f"\nPROMOTED docs/{rel}. mkdocs.yml updated; gate green.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
