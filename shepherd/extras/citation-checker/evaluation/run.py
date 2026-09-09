"""Run the frozen development selection through the installed checker."""

import argparse
import json
from pathlib import Path

from score import evaluate
from shepherd_citation_checker import CheckerConfig, check_references
from shepherd_citation_checker.api import verify


def main() -> None:
    """Keep labels in the evaluator and give the checker only citations/evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Fresh run directory")
    parser.add_argument("--prepared-run", type=Path, help="Reuse only the initial evidence of a saved package run")
    parser.add_argument("--evidence-only", action="store_true", help="Disable discovery; requires --prepared-run")
    args = parser.parse_args()
    if args.evidence_only and args.prepared_run is None:
        parser.error("--evidence-only requires --prepared-run")
    frozen = Path(__file__).parent / "frozen"
    refs = json.loads((frozen / "inputs.json").read_text())["references"]
    labels = json.loads((frozen / "labels.json").read_text())
    evidence = None
    if args.prepared_run is not None:
        root = args.prepared_run.resolve()
        verify(root)
        request = json.loads((root / "request.json").read_text())
        if {r["id"]: r["raw"] for r in request["references"]} != {r["id"]: r["raw"] for r in refs}:
            raise ValueError("Prepared run citations differ from the frozen selection")
        evidence = {key: [root / e["path"] for e in entries] for key, entries in request["evidence"].items()}
    report = check_references(
        refs,
        args.output,
        evidence=evidence,
        retrieve_initial=args.prepared_run is None,
        config=CheckerConfig(searches=0, fetches=0) if args.evidence_only else CheckerConfig(),
    )
    (args.output / "evaluation.json").write_text(json.dumps(evaluate(report, labels), indent=2) + "\n")


if __name__ == "__main__":
    main()
