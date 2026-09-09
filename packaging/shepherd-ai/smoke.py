"""Check an installed public distribution from outside the source checkout.

Run with the clean environment's Python and -I. --base-only checks discovery
without optional parsers; the default also executes and resumes an exact match.
"""

# ruff: noqa: INP001 -- standalone release validation script
from __future__ import annotations

import argparse
import json
import tempfile
from importlib import metadata
from pathlib import Path


def main() -> None:
    """Validate base or citation-extra installation without network/model calls."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-only", action="store_true")
    args = parser.parse_args()
    from shepherd_core.package import discover_packages

    import shepherd

    version = metadata.version("shepherd-ai")
    assert shepherd.__version__ == version
    assert "citation_checker" in discover_packages()
    assert any(e.name == "shepherd-check-citations" for e in metadata.entry_points(group="console_scripts"))
    from shepherd_citation_checker import CheckerConfig, check_references
    from shepherd_citation_checker.api import resume

    with tempfile.TemporaryDirectory(prefix="citation-wheel-smoke-") as directory:
        root = Path(directory)
        reference = {
            "id": "1",
            "raw": "title: Example work\nauthors: Jane Doe\nyear: 2024\nvenue: Journal of Examples\ndoi: 10.1234/one",
        }
        if args.base_only:
            try:
                check_references([reference], root / "run", retrieve_initial=False)
            except RuntimeError as exc:
                assert "shepherd-ai[citation-checker]" in str(exc)  # noqa: PT017 -- no pytest in clean installs
                assert not (root / "run").exists()
            else:
                raise AssertionError("Base-only environment unexpectedly has the parser extra")
        else:
            source = root / "source.json"
            source.write_text(
                json.dumps(
                    {
                        "url": "https://api.crossref.org/works/10.1234/one",
                        "http_status": 200,
                        "body": json.dumps(
                            {
                                "message": {
                                    "title": ["Example work"],
                                    "author": [{"given": "Jane", "family": "Doe"}],
                                    "published": {"date-parts": [[2024]]},
                                    "container-title": ["Journal of Examples"],
                                    "DOI": "10.1234/one",
                                    "type": "journal-article",
                                }
                            }
                        ),
                    }
                )
            )
            report = check_references(
                [reference],
                root / "run",
                evidence={"1": [source]},
                retrieve_initial=False,
                config=CheckerConfig(searches=0, fetches=0),
            )
            assert report["complete"]
            assert report["results"][0]["metadata_status"] == "verified"
            assert resume(root / "run") == report
            graph = json.loads((root / "run/task-graph.json").read_text())
            assert len(graph["stage_runs"]) == 4
            for name in ("report.json", "report.md", "corrections.bib", "task-trace.json"):
                assert (root / "run" / name).exists(), name
    print(json.dumps({"version": version, "base_only": args.base_only, "status": "passed"}))


if __name__ == "__main__":
    main()
