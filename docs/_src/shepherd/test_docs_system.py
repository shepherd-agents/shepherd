"""The DESIGN Phase-1 negative tests, as a permanent module: the gate and
drift checks must FAIL on seeded violations (a green gate is meaningless
otherwise)."""

import shutil
import subprocess
import sys
from pathlib import Path

PROTO = Path(__file__).resolve().parent.parent.parent.parent # repo root (docs/_src/shepherd/ -> up 4; content: docs/shepherd, docs/_generated)
TOOLING = PROTO / "docs/_system" # docs-system tooling now lives here (mkdocs.yml, scripts/)
GATE = TOOLING / "scripts/check_shepherd_docs.py"


def _gate(root: Path) -> int:
    return subprocess.run(
        [sys.executable, str(GATE), "--root", str(root)],
        capture_output=True, text=True,
    ).returncode


def _seed(tmp_path: Path) -> Path:
    root = tmp_path / "proto"
    root.mkdir()
    (root / "docs").mkdir()
    shutil.copytree(PROTO / "docs/shepherd", root / "docs/shepherd")
    # Copy the real config but normalize docs_dir to the in-fixture single-root
    # layout: the deployed config uses '../shepherd' because the tooling
    # lives in docs/_system/ while the content stays at the repo root.
    cfg = (TOOLING / "mkdocs.yml").read_text(encoding="utf-8")
    cfg = cfg.replace("docs_dir: ../shepherd", "docs_dir: docs/shepherd")
    (root / "mkdocs.yml").write_text(cfg, encoding="utf-8")
    return root


def test_baseline_gate_green(tmp_path):
    assert _gate(_seed(tmp_path)) == 0


def test_unlabeled_page_fails_meta(tmp_path):
    root = _seed(tmp_path)
    (root / "docs/shepherd/probe.md").write_text("# Probe\n\nno metadata block\n", encoding="utf-8")
    assert _gate(root) == 1


def test_unexcluded_scaffold_fails_leak(tmp_path):
    root = _seed(tmp_path)
    cfg = (root / "mkdocs.yml").read_text(encoding="utf-8")
    # Un-exclude a scaffold page without promoting it -> LEAK (+CONFLICT).
    # (Uses concepts/placements.md — still a scaffold; start/install was promoted.)
    cfg = cfg.replace("  !/index.md\n", "  !/index.md\n  !/concepts/placements.md\n")
    (root / "mkdocs.yml").write_text(cfg, encoding="utf-8")
    assert _gate(root) == 1


def test_tampered_snapshot_fails_drift(tmp_path):
    snap = PROTO / "docs/_generated/shepherd/python-api/public-symbols.json"
    backup = snap.read_text(encoding="utf-8")
    try:
        snap.write_text(backup + "\n", encoding="utf-8")
        rc = subprocess.run(
            [sys.executable, str(TOOLING / "scripts/gen_shepherd_api_inventory.py"), "--check"],
            capture_output=True, text=True, cwd=str(TOOLING / "scripts"),
        ).returncode
        assert rc == 1
    finally:
        snap.write_text(backup, encoding="utf-8", newline="")
