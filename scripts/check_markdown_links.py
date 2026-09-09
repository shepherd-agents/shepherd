#!/usr/bin/env python3
"""Check local Markdown links under a documentation tree.

By default this validates the active vcs-core design reading path while
skipping historical areas that intentionally preserve older references.

Run from the workspace root:
    ./scripts/check_markdown_links.py
    ./scripts/check_markdown_links.py --project vcs-core
    ./scripts/check_markdown_links.py --project vcs-core --include-history
    ./scripts/check_markdown_links.py --root vcs-core/design --exclude archive --exclude history
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from _workspace_layout import project_docs_dir, require_workspace_root

REPO_ROOT = require_workspace_root(Path(__file__))
LINK_RE = re.compile(r"!?\[[^\]]+\]\(([^)]+)\)")
FENCED_CODE_BLOCK_RE = re.compile(r"(^|\n)(```|~~~).*?\n\2\s*(?=\n|$)", re.DOTALL)
DEFAULT_PROJECT = "vcs-core"
DEFAULT_EXCLUDES = ("archive", "history")
MAINTAINED_HISTORY_EXCLUDES = ("archived-proposals", "dated-notes")


def _iter_markdown_files(root: Path, excludes: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*.md"):
        rel = path.relative_to(root)
        if any(part in excludes for part in rel.parts):
            continue
        files.append(path)
    return sorted(files)


def _normalize_target(raw_target: str) -> str | None:
    target = raw_target.strip()
    if not target:
        return None
    if target.startswith(("#", "mailto:", "http://", "https://")):
        return None
    target = target.split("#", 1)[0].strip()
    if not target:
        return None
    return target


def _check_links(root: Path, excludes: tuple[str, ...]) -> list[str]:
    errors: list[str] = []
    for md_path in _iter_markdown_files(root, excludes):
        text = FENCED_CODE_BLOCK_RE.sub(r"\1", md_path.read_text())
        for target in LINK_RE.findall(text):
            normalized = _normalize_target(target)
            if normalized is None:
                continue
            resolved = (md_path.parent / normalized).resolve()
            if resolved.exists():
                continue
            errors.append(f"{md_path.relative_to(REPO_ROOT)}: broken local link '{target}'")
    return errors


def _dedupe_preserving_order(items: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return tuple(result)


def _effective_excludes(*, includes_history: bool, excludes: list[str]) -> tuple[str, ...]:
    effective = list(excludes)
    if includes_history:
        effective = [item for item in effective if item != "history"]
        effective.extend(MAINTAINED_HISTORY_EXCLUDES)
    return _dedupe_preserving_order(effective)


def _resolve_default_root(project: str) -> Path:
    kind = "docs" if project == "shepherd" else "design"
    root = project_docs_dir(REPO_ROOT, project=project, kind=kind)
    if root is None and project == "shepherd":
        public_docs = REPO_ROOT / "docs" / "shepherd"
        if public_docs.is_dir():
            root = public_docs
    if root is None:
        raise SystemExit(
            f"Could not find an isolated {kind} root for project {project!r} in the current repo layout. "
            "Use --root explicitly."
        )
    return root


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--project",
        choices=("shepherd", "vcs-core"),
        default=DEFAULT_PROJECT,
        help="Project whose design tree should be scanned when --root is omitted.",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="Documentation root to scan relative to the repo root.",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=list(DEFAULT_EXCLUDES),
        help="Directory name to exclude anywhere below the root. May be repeated.",
    )
    parser.add_argument(
        "--include-history",
        action="store_true",
        help=(
            "Include maintained history when scanning vcs-core docs. "
            "Archived proposals and dated notes remain excluded."
        ),
    )
    args = parser.parse_args()

    root = (REPO_ROOT / args.root).resolve() if args.root else _resolve_default_root(args.project)
    excludes = _effective_excludes(
        includes_history=args.include_history,
        excludes=list(args.exclude),
    )

    if not root.exists():
        print(f"Root does not exist: {root}", file=sys.stderr)
        return 2

    errors = _check_links(root, excludes)
    if errors:
        print(f"Found {len(errors)} broken local Markdown link(s):\n")
        for error in errors:
            print(f"- {error}")
        return 1

    rel_root = root.relative_to(REPO_ROOT)
    excluded = ", ".join(excludes) if excludes else "(none)"
    print(f"All Markdown links passed under {rel_root} (excluding: {excluded}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
