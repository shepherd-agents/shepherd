"""One command for PDF audits, prepared references and frozen-run execution."""

import argparse
import json
from pathlib import Path

# ruff: noqa: T201 -- tool and CLI response protocol
from typing import Any

from .api import check_paper, check_references, resume
from .config import CheckerConfig


def main() -> Any:
    """Run the installed citation-checker command."""
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    paper = subs.add_parser("paper")
    paper.add_argument("pdf", type=Path)
    refs = subs.add_parser("references")
    refs.add_argument(
        "input",
        type=Path,
        help="JSON object containing references; optional prefetched evidence paths are relative to this file",
    )
    refs.add_argument("--evidence-only", action="store_true", help="Disable initial and reviewer retrieval")
    for p in (paper, refs):
        p.add_argument("--output", required=True, type=Path)
        p.add_argument("--batch-size", type=int, default=6)
        p.add_argument("--workers", type=int, default=5)
    run = subs.add_parser("run")
    run.add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.command != "run":
        from .bundle import require_dependencies

        try:
            require_dependencies()
        except RuntimeError as exc:
            parser.error(str(exc))
    if args.command == "run":
        report = resume(args.directory)
    else:
        offline = getattr(args, "evidence_only", False)
        config = CheckerConfig(
            batch_size=args.batch_size, workers=args.workers, searches=0 if offline else 6, fetches=0 if offline else 12
        )
        if args.command == "paper":
            report = check_paper(args.pdf, args.output, config=config)
        else:
            data = json.loads(args.input.read_text())
            evidence = {
                r["id"]: [args.input.resolve().parent / e["evidence_file"] for e in r.get("prefetched", [])]
                for r in data["references"]
            }
            report = check_references(
                data["references"], args.output, evidence=evidence, config=config, retrieve_initial=not offline
            )
    print(
        json.dumps(
            {
                "references": report["reference_count"],
                "complete": report["complete"],
                "execution": report["execution_counts"],
            }
        )
    )
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
