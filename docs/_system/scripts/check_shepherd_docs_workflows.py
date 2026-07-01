#!/usr/bin/env python3
"""Workflow-fixture validator ( 'Workflow Documentation').

Validates every docs/_src/shepherd/workflows/fixtures/*.yaml: required keys are
present and `docs_path` points at an existing workflow page under docs/shepherd.
An explicit EMPTY_FIXTURES sentinel (and no real fixtures) is allowed per the
scaffold-phase definition of done. Exit 0 ok / 1 violation. ASCII output.
"""

from __future__ import annotations

from pathlib import Path

import yaml

PROTO = Path(__file__).resolve().parents[3] # repo root (content lives at repo root; tooling in docs_system/)
FIXTURES = PROTO / "docs/_src/shepherd/workflows/fixtures"
DOCS = PROTO / "docs/shepherd"

# Minimum keys a fixture must carry to back a public workflow page.
REQUIRED = [
    "fixture_version", "source_state", "workflow_id", "docs_path",
    "display_name", "short_description", "install_command", "run_command",
]


def main() -> int:
    fixtures = sorted(FIXTURES.glob("*.yaml"))
    if not fixtures:
        if (FIXTURES / "EMPTY_FIXTURES").exists():
            print("ok: no fixtures (EMPTY_FIXTURES sentinel present)")
            return 0
        print("ERROR: no workflow fixtures and no EMPTY_FIXTURES sentinel")
        return 1

    errors: list[str] = []
    for fx in fixtures:
        data = yaml.safe_load(fx.read_text(encoding="utf-8")) or {}
        missing = [k for k in REQUIRED if k not in data]
        if missing:
            errors.append(f"{fx.name}: missing required keys: {', '.join(missing)}")
            continue
        docs_path = str(data["docs_path"])
        if not (DOCS / docs_path).is_file():
            errors.append(f"{fx.name}: docs_path '{docs_path}' has no page at docs/shepherd/{docs_path}")

    for e in errors:
        print("ERROR " + e)
    print(f"\nworkflow-fixtures: {len(fixtures)} fixture(s), errors={len(errors)}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
