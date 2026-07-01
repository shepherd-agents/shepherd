#!/usr/bin/env python3
"""Membership gate (DESIGN 1.6 / §5.2) — the default-deny enforcement.

Invariants: META, ADMIT, CONFLICT, LEAK, ORPHAN, MODELINE (+C4), and
--assert-built (page-set equality + styled-site). Exit 0 ok / 1 violation /
2 crash-usage. ASCII output. Tag-tolerant YAML loader (the emoji
!!python/name tags crash plain safe_load); exclude_docs matched with
pathspec's GitIgnoreSpec — the exact class MkDocs 1.6 uses.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml
from pathspec.gitignore import GitIgnoreSpec

PROTO = Path(__file__).resolve().parent.parent

STATUSES = {"release-ready", "scaffold", "fast-follow", "historical-reference"}
STATES = {"shipped-source", "checked-example", "checked-fixture", "generated", "preview", "scaffold", "historical"}
ADMISSIBLE = {"shipped-source", "checked-example", "checked-fixture", "generated", "preview"}
KEYS = ("Page status", "Source state", "Applies to", "Owner", "Validation")
META_EXEMPT_PREFIX = ("_templates/",)
META_EXEMPT = {"_runbook.md"}

MODE_LINES = {
    "quickstart": "*Quickstart.",
    "tutorial": "*Tutorial.",
    "guide": "*How-to guide.",
    "concept": "*Concept.",
    "reference": "*Reference.",
    "workflow": "*Workflow.",
    "inventory": "*Source-state inventory.",
    "operator-stub": "*Operators.",
}
TYPE_BY_DIR = {
    "start": "quickstart", "tutorials": "tutorial", "guides": "guide",
    "concepts": "concept", "reference": "reference", "workflows": "workflow",
}
TYPE_OVERRIDES = {"reference/source-state.md": "inventory", "workflows/index.md": "operator-stub"}
MODELINE_EXEMPT = {"index.md", "_runbook.md"}
RUNBOOK = "see docs/shepherd/_runbook.md"


class TolerantLoader(yaml.SafeLoader):
    """SafeLoader that maps UNKNOWN tags to inert strings (DESIGN §5.2).

    Security note: this constructs NO Python objects for custom tags — the
    fallback constructor below returns a plain string placeholder, so it is
    strictly safer than crashing and never executes tag-directed code. It
    exists because mkdocs.yml legitimately carries `!!python/name:` tags (the
    Material emoji extension), on which plain yaml.safe_load raises
    ConstructorError (probe-proven, 2026-06-13).
    """


TolerantLoader.add_constructor(None, lambda loader, node: f"<tag:{node.tag}>")


def load_config(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=TolerantLoader) or {}


def nav_files(nav) -> list[str]:
    out: list[str] = []
    if isinstance(nav, str):
        if nav.endswith(".md"):
            out.append(nav)
    elif isinstance(nav, list):
        for item in nav:
            out += nav_files(item)
    elif isinstance(nav, dict):
        for v in nav.values():
            out += nav_files(v)
    return out


def parse_meta(text: str) -> dict[str, str]:
    meta = {}
    for line in text.splitlines()[:40]:
        m = re.match(r"^> ([A-Za-z -]+): (.*)$", line.strip())
        if m:
            meta[m.group(1).strip()] = m.group(2).strip()
    return meta


def page_type(rel: str) -> str | None:
    if rel in TYPE_OVERRIDES:
        return TYPE_OVERRIDES[rel]
    if rel in MODELINE_EXEMPT:
        return None
    top = rel.split("/", 1)[0]
    return TYPE_BY_DIR.get(top)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(PROTO))
    ap.add_argument("--config", default=None)
    ap.add_argument("--assert-built", dest="site", default=None)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()

    root = Path(args.root)
    cfg_path = Path(args.config) if args.config else root / "mkdocs.yml"
    try:
        cfg = load_config(cfg_path)
    except Exception as exc:
        print(f"CRASH loading {cfg_path}: {type(exc).__name__}: {exc}")
        return 2

    docs_dir = root / cfg.get("docs_dir", "docs")
    nav = nav_files(cfg.get("nav"))
    excl_lines = [ln for ln in (cfg.get("exclude_docs") or "").splitlines() if ln.strip()]
    spec = GitIgnoreSpec.from_lines(excl_lines)

    pages: dict[str, dict] = {}
    for md in sorted(docs_dir.rglob("*.md")):
        rel = md.relative_to(docs_dir).as_posix()
        if rel.startswith(META_EXEMPT_PREFIX) or rel in META_EXEMPT:
            continue
        pages[rel] = {"meta": parse_meta(md.read_text(encoding="utf-8")),
                      "text": md.read_text(encoding="utf-8"),
                      "excluded": bool(spec.match_file(rel))}

    errors: list[str] = []

    def err(inv: str, rel: str, msg: str, fix: str) -> None:
        errors.append(f"[{inv}] {rel}: {msg}  (fix: {fix}; {RUNBOOK})")

    for rel, p in pages.items():
        meta, status = p["meta"], p["meta"].get("Page status")
        missing = [k for k in KEYS if k not in meta]
        if missing:
            err("META", rel, f"missing metadata: {', '.join(missing)}", "copy the block from a _template")
            continue
        if status not in STATUSES or meta["Source state"] not in STATES:
            err("META", rel, "invalid Page status / Source state value", "use the §5.1 enums")
        # MODELINE
        ptype = page_type(rel)
        if ptype and MODE_LINES[ptype] not in p["text"]:
            err("MODELINE", rel, f"canonical {ptype} mode line missing", "copy it from the matching page-mode template")
        # (C4 '## Going deeper' footer requirement dropped — internal repo-reference
        #  footers are not carried on the public concept pages; see docs sync 2026-07-01.)
        # LEAK
        if status != "release-ready" and not p["excluded"]:
            err("LEAK", rel, f"{status} page is un-excluded (URL-reachable in public build)",
                "remove its '!' line from exclude_docs or promote it (S4)")
        # CONFLICT / ORPHAN
        if not p["excluded"] and rel not in nav:
            inv = "ORPHAN" if status == "release-ready" else "CONFLICT"
            err(inv, rel, "un-excluded but absent from nav (built + unlisted)",
                "add the nav entry or re-exclude")

    for rel in nav:
        if rel not in pages:
            err("CONFLICT", rel, "nav entry has no such page", "fix the nav path")
            continue
        p = pages[rel]
        if p["excluded"]:
            err("CONFLICT", rel, "in nav but excluded from the build", "add the '!/...' un-exclusion")
        meta = p["meta"]
        if meta.get("Page status") != "release-ready" or meta.get("Source state") not in ADMISSIBLE \
                or meta.get("Validation", "").lower() in ("", "not yet validated"):
            err("ADMIT", rel, "nav page is not admissible (status/state/validation)",
                "complete the promotion triple (S4)")

    if args.site:
        site = Path(args.site)
        expected = {"404.html"}
        for rel in [r for r, p in pages.items() if not p["excluded"]]:
            expected.add("index.html" if rel == "index.md" else re.sub(r"(index)?\.md$", "", rel).rstrip("/") + "/index.html")
        actual = {h.relative_to(site).as_posix() for h in site.rglob("*.html")
                  if not h.relative_to(site).as_posix().startswith("assets/")}
        for extra in sorted(actual - expected):
            err("ASSERT-BUILT", extra, "page reachable in public build but not sanctioned", "default-deny backstop: find the stray un-exclusion")
        for miss in sorted(expected - actual):
            err("ASSERT-BUILT", miss, "expected page missing from public build", "check nav/exclude/strict-build output")
        if not any((site / "assets").rglob("*")):
            err("ASSERT-BUILT", "assets/", "theme assets empty (site would render unstyled)", "exclude_docs must keep '!assets/**'")
        if not (site / "stylesheets/extra.css").exists():
            err("ASSERT-BUILT", "stylesheets/extra.css", "brand CSS missing from built site", "exclude_docs must keep '!stylesheets/**'")

    for e in errors:
        print("ERROR " + e)
    print(f"\ngate: {len(pages)} page(s), nav={len(nav)}, errors={len(errors)}")
    return 1 if errors else 0


def self_test() -> int:
    import shutil
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        shutil.copytree(PROTO / "docs", root / "docs")
        shutil.copy(PROTO / "mkdocs.yml", root / "mkdocs.yml")
        (root / "docs/probe.md").write_text("# Probe\n\nno metadata\n", encoding="utf-8")
        rc = subprocess.call([sys.executable, __file__, "--root", str(root)])
        if rc != 1:
            print(f"SELF-TEST FAIL: seeded violation returned {rc}, expected 1")
            return 1
    print("SELF-TEST PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
