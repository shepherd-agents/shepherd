#!/usr/bin/env python3
"""Generate the per-symbol API reference pages (DESIGN §5.3 page contract).

Pulls from the REAL repo facade (read-only). Pages are committed; --check
regenerates in memory and fails on any difference. `_map.yml` is INPUT (the
curated See-also map) — never rewritten here.
"""

from __future__ import annotations

import sys

from _facade import API_DIR, FACADE_IMPORT, MAP_FILE, facade_map, page_filename, symbol_info

MODE_LINE = (
    "*Reference. Exact, generated facts. The mental model lives in "
    "concepts, recipes in guides.*"
)


def load_map() -> dict:
    if not MAP_FILE.exists():
        return {}
    import yaml

    return yaml.safe_load(MAP_FILE.read_text(encoding="utf-8")) or {}


def render(info: dict, see_also: dict | None) -> str:
    name, target, kind = info["name"], info.get("target", info["source"]), info["kind"]
    lines = [
        f"# `{FACADE_IMPORT}.{name}`",
        "",
        "> Page status: scaffold",
        "> Source state: generated",
        "> Applies to: Shepherd v0.3.0",
        "> Owner: @docs-system-owner (TBD)",
        "> Validation: scripts/gen_shepherd_api_inventory.py --check",
        "",
        MODE_LINE,
        "",
        f'<span class="api-kind">{kind}</span>',
        "",
    ]
    if kind == "handle-surface (runtime-resolved)":
        # The lazily-imported handle/grant surface cannot be autodoc-rendered by
        # the import-light docs build (and `May` IS `typing.Annotated` under an
        # alias, which griffe cannot resolve at all) — emit static facts instead
        # of a `:::` directive that breaks the strict internal build.
        lines += [
            f"`{FACADE_IMPORT}.{name}` is part of the workspace-handle surface: it is",
            "resolved lazily at runtime because its implementation imports the",
            "substrate engine, which the offline docs build does not load.",
            "",
            f"- Runtime source: `{target}`",
            "- Usage and semantics: [Permissions](../../concepts/permissions.md)",
            "  and the run/output/settlement examples in the guides.",
            "",
        ]
    else:
        lines += [
            f"::: {target}",
            "    options:",
            "      show_root_heading: true",
            "      heading_level: 2",
            "      show_root_full_path: false",
            "",
        ]
    if see_also:
        lines += ["## See also", ""]
        if see_also.get("concept"):
            lines.append(f"- Mental model: [{see_also['concept']}](../../{see_also['concept']})")
        if see_also.get("guide"):
            lines.append(f"- Recipe: [{see_also['guide']}](../../{see_also['guide']})")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    check = "--check" in sys.argv
    exports, _ = facade_map()
    infos = symbol_info()
    smap = load_map()
    stale = []
    API_DIR.mkdir(parents=True, exist_ok=True)
    expected: set[str] = set()
    for info in infos:
        fn = API_DIR / page_filename(info["name"], exports)
        expected.add(fn.name)
        content = render(info, smap.get(info["name"]))
        if check:
            if not fn.exists() or fn.read_text(encoding="utf-8") != content:
                stale.append(fn.name)
        else:
            fn.write_text(content, encoding="utf-8", newline="\n")
    # Orphans: committed api/*.md pages whose symbol left the facade __all__.
    # --check flags them as drift; write mode prunes them, so the reference can
    # never silently document a symbol the code no longer exports.
    orphans = sorted(p.name for p in API_DIR.glob("*.md") if p.name not in expected)
    if check:
        problems = sorted(stale) + [f"{o} (orphan)" for o in orphans]
        if problems:
            print(f"DRIFT: {len(problems)} generated page(s) stale/orphan: {', '.join(problems)}")
            print("fix: ./check.sh regen   (see docs/_runbook.md)")
            return 1
        print(f"ok: {len(infos)} generated pages match the facade")
        return 0
    for name in orphans:
        (API_DIR / name).unlink()
    msg = f"wrote {len(infos)} pages -> {API_DIR}"
    if orphans:
        msg += f"; pruned {len(orphans)} orphan(s): {', '.join(orphans)}"
    print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
