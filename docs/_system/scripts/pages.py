#!/usr/bin/env python3
"""pages.py — the documentation page map, as live state.

Reads `pages.yml` (what each page IS and who produces it) and reports each
page's REAL state: does the file exist, is it public (in the nav), what's its
status, and what code/test backs it. This is the executable answer to "where is
each page defined, and what produces it".

Usage (via the entry point):
    run.py pages            # full table
    run.py pages quickstart # only pages whose path matches the filter

Producers:
  reference — generated from the code facade (one api page per public symbol).
  authored  — written prose (draft -> fact-check -> readability -> sign-off).
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent  # scripts/
PROTO = HERE.parent
sys.path.insert(0, str(HERE))  # so we can reuse the gate + facade helpers

import yaml
from check_shepherd_docs import load_config, nav_files, parse_meta

DOCS = PROTO.parent.parent / "docs/shepherd"  # content at repo root; PROTO (docs_system/) holds mkdocs.yml + pages.yml


def public_pages() -> set[str]:
    """Pages reachable in the PUBLIC build = the nav allowlist (relative to docs/)."""
    cfg = load_config(PROTO / "mkdocs.yml")
    return set(nav_files(cfg.get("nav")))


def status_of(rel: str) -> str:
    f = DOCS / rel
    if not f.exists():
        return "MISSING"
    return parse_meta(f.read_text(encoding="utf-8")).get("Page status", "?")


def reference_set() -> list[str]:
    """Expected reference pages, enumerated from the code (fallback: the dir)."""
    try:
        from _facade import facade_map, page_filename, symbol_info

        exports, _ = facade_map()
        pages = [f"reference/api/{page_filename(i['name'], exports)}" for i in symbol_info()]
    except Exception:
        api = DOCS / "reference/api"
        pages = sorted(p.relative_to(DOCS).as_posix() for p in api.glob("*.md")) if api.exists() else []
    pages.append("reference/cli.md")
    return pages


def main() -> int:
    filt = sys.argv[1] if len(sys.argv) > 1 else ""
    pub = public_pages()
    manifest = yaml.safe_load((PROTO / "pages.yml").read_text(encoding="utf-8")) or {}

    rows: list[tuple[str, str, str, str, str]] = []  # page, producer, status, public, backing

    for rel in reference_set():
        rows.append((rel, "reference", status_of(rel), "yes" if rel in pub else "no", "generated from code"))

    for item in manifest.get("authored", []):
        rel = item["page"].removeprefix("docs/shepherd/")
        backing = "prose"
        if item.get("code"):
            backing = f"code: {item['code']}"
            if item.get("test"):
                backing += f"  test: {item['test']}"
        rows.append((rel, "authored", status_of(rel), "yes" if rel in pub else "no", backing))

    for item in manifest.get("commission", []):
        rel = item["page"].removeprefix("docs/shepherd/")
        rows.append((rel, "authored", "TO-COMMISSION", "no", item.get("title", "")))

    rows = [r for r in rows if filt in r[0]]
    width = max((len(r[0]) for r in rows), default=4)

    print(f"{'PAGE'.ljust(width)}  {'PRODUCER':9}  {'STATUS':14}  PUBLIC  BACKING")
    print(f"{'-' * width}  {'-' * 9}  {'-' * 14}  ------  {'-' * 20}")
    for page, producer, status, public, backing in rows:
        print(f"{page.ljust(width)}  {producer:9}  {status:14}  {public:6}  {backing}")

    public_count = sum(1 for r in rows if r[3] == "yes")
    print(f"\n{len(rows)} page(s) shown - {public_count} public.")
    print("Produce/validate: `run.py check` (generate + drift + gates + tests + build)")
    print("Publish one page: `run.py promote <page>`")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
