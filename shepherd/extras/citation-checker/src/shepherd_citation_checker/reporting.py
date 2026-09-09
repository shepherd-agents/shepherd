"""One report projection, independent of benchmark datasets and labels."""

from collections import Counter
from typing import Any

from ._engine.bibtex import render_citation
from ._engine.checkpoints import finalize
from ._engine.summary import assessment
from ._engine.validation import unresolved
from .storage import read, write


def build(root) -> Any:
    """Build the retained output artifact."""
    refs = read(root / "citations.json")["references"]
    rows = {r["id"]: r for r in read(root / "deterministic.json")["results"]}
    attempts = []
    for name in read(root / "routing.json")["batches"]:
        from pathlib import Path

        job = Path(name)
        if not (job / "receipt.json").exists():
            continue
        receipt = read(job / "receipt.json")
        # Reapply the same validator to retained, sealed raw decisions.
        checked = finalize(job, read(job / "request.json"), dict(receipt))
        if checked.get("results") != receipt.get("results"):
            raise ValueError("Saved assessment differs from retained decision")
        attempts.append(receipt)
        for row in receipt.get("results", []):
            rows[row["id"]] = {
                **row,
                "execution_status": receipt["reference_execution"][row["id"]],
                "review_method": "opus",
                "batch_path": str(job),
            }
    results = []
    for ref in refs:
        row = rows.get(ref["id"], {**unresolved(ref, "Execution incomplete"), "execution_status": "not_completed"})
        row.update(raw=ref["raw"], pages=ref.get("pages", []), citation_assessment=assessment(row))
        results.append(row)
    report = {
        "schema_version": "3.0",
        "results": results,
        "reference_count": len(refs),
        "complete": all(r["execution_status"] == "completed" for r in results),
        "execution_counts": dict(Counter(r["execution_status"] for r in results)),
        "attempts": attempts,
    }
    markdown = ["# Citation report", "", f"{len(refs)} references. Execution complete: {report['complete']}.", ""]
    corrections = ["% Supported proposed corrections; consult report.md and retained evidence."]
    for row in results:
        markdown.extend(
            [
                f"## Reference {row['id']}",
                "",
                row["raw"],
                "",
                row["citation_assessment"]["headline"],
                "",
                row["reason"],
                "",
            ]
        )
        markdown.extend(f"- {field}: {c['outcome']} — {c['explanation']}" for field, c in row["field_checks"].items())
        markdown.extend(["", row.get("correction_note", ""), ""])
        if row.get("corrected_citation"):
            entry = render_citation(row["corrected_citation"], row["id"])
            corrections.append(entry)
            markdown.extend(["```bibtex", entry, "```", ""])
        markdown.extend(f"- [{e['supports']}]({e['url']})" for e in row["evidence"])
        markdown.append("")
    write(root / "report.json", report)
    (root / "report.md").write_text("\n".join(markdown))
    (root / "corrections.bib").write_text("\n\n".join(corrections) + "\n")
    return report
