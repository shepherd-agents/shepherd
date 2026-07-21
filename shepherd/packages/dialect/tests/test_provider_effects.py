from __future__ import annotations

from typing import TYPE_CHECKING

from shepherd_dialect.provider_effects import (
    reconcile_provider_file_claims,
    snapshot_workspace_files,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_file_claim_reconciliation_distinguishes_all_evidence_classes(tmp_path: Path) -> None:
    (tmp_path / "confirmed.txt").write_text("before\n", encoding="utf-8")
    (tmp_path / "deleted.txt").write_text("before\n", encoding="utf-8")
    before = snapshot_workspace_files(tmp_path)

    (tmp_path / "confirmed.txt").write_text("after\n", encoding="utf-8")
    (tmp_path / "deleted.txt").unlink()
    (tmp_path / "carrier-only.txt").write_text("created\n", encoding="utf-8")
    after = snapshot_workspace_files(tmp_path)

    result = reconcile_provider_file_claims(
        before=before,
        after=after,
        provider_paths=("confirmed.txt", "provider-only.txt"),
    )

    assert result["classification_counts"] == {
        "carrier_confirmed": 1,
        "provider_only": 1,
        "carrier_only": 2,
    }
    assert {tuple(record.values()) for record in result["records"]} == {
        ("confirmed.txt", "carrier_confirmed"),
        ("provider-only.txt", "provider_only"),
        ("carrier-only.txt", "carrier_only"),
        ("deleted.txt", "carrier_only"),
    }
