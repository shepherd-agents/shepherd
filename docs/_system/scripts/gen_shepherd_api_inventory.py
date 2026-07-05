#!/usr/bin/env python3
"""Enriched public-symbol snapshot + drift check (DESIGN §5.3).

{name, source, kind, signature, doc_hash} per symbol — so drift fires on
renames, signature changes, AND docstring edits. Byte-compared in --check.
"""

from __future__ import annotations

import json
import sys

from _facade import FACADE_IMPORT, SNAPSHOT, symbol_info


def render() -> str:
    infos = sorted(symbol_info(), key=lambda i: i["name"])
    doc = {
        "facade": FACADE_IMPORT,
        "generator": "scripts/gen_shepherd_api_inventory.py",
        "note": "Generated from the shipped `shepherd` facade __all__; the handle/grant surface is listed as runtime-resolved (offline docs build cannot import it).",
        "symbol_count": len(infos),
        "symbols": {i["name"]: {k: i[k] for k in ("source", "kind", "signature", "doc_hash")} for i in infos},
    }
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    content = render()
    if "--check" in sys.argv:
        if not SNAPSHOT.exists():
            print(f"DRIFT: snapshot missing: {SNAPSHOT}")
            return 1
        if SNAPSHOT.read_text(encoding="utf-8") != content:
            print("DRIFT: public-symbols.json is stale (facade changed).")
            print("fix: ./check.sh regen   (see docs/_runbook.md)")
            return 1
        print("ok: snapshot matches the facade")
        return 0
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(content, encoding="utf-8", newline="\n")
    print(f"wrote {SNAPSHOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
