"""Installed-package entry points and immutable run preparation."""

import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .config import CheckerConfig
from .storage import digest, read, write


def prepare(output_dir, *, pdf=None, references=None, evidence=None, config=None, retrieve_initial=True) -> Any:
    """Freeze package code, inputs and parser dependencies in a fresh run."""
    root = Path(output_dir).resolve()
    if root.exists():
        raise ValueError("Choose a fresh output directory; completed runs are immutable")
    config = config or CheckerConfig()
    if (pdf is None) == (references is None):
        raise ValueError("Supply either a PDF or reference objects")
    from .bundle import build, require_dependencies

    require_dependencies()
    root.mkdir(parents=True)
    source = Path(__file__).parent
    shutil.copytree(
        source, root / "code/shepherd_citation_checker", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    versions = build(root / "code/dependencies.zip")
    seeds = {}
    for identifier, paths in (evidence or {}).items():
        seeds[identifier] = []
        for path in paths:
            source_path = Path(path).resolve()
            record = read(source_path)
            if not isinstance(record, dict) or not isinstance(record.get("url"), str):
                raise TypeError("Evidence must retain its URL and response")
            checksum = digest(source_path)
            target = root / "evidence" / (checksum + ".json")
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(source_path, target)
            seeds[identifier].append({"path": target.relative_to(root).as_posix(), "sha256": checksum})
    if pdf is not None:
        target = root / "input/source.pdf"
        target.parent.mkdir()
        shutil.copyfile(Path(pdf).resolve(), target)
    refs = (
        [{"id": r["id"], "raw": r["raw"], "pages": r.get("pages", [])} for r in references]
        if references is not None
        else None
    )
    request = {
        "pdf": str(pdf) if pdf is not None else None,
        "references": refs,
        "evidence": seeds,
        "config": config.to_dict(),
        "retrieve_initial": retrieve_initial,
    }
    write(root / "request.json", request)
    files = [root / "request.json", *(root / "code").rglob("*")]
    if pdf is not None:
        files.append(root / "input/source.pdf")
    write(
        root / "manifest.json",
        {
            "package": "shepherd-citation-checker",
            "version": __version__,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "model": "opus",
            "dependencies": versions,
            "sha256": {p.relative_to(root).as_posix(): digest(p) for p in files if p.is_file()},
        },
    )
    return root


def verify(root) -> Any:
    """Check frozen code, inputs and initial evidence against their hashes."""
    root = Path(root)
    for name, expected in read(root / "manifest.json")["sha256"].items():
        if digest(root / name) != expected:
            raise ValueError(f"Frozen input/code changed: {name}")
    for values in read(root / "request.json")["evidence"].values():
        for item in values:
            if digest(root / item["path"]) != item["sha256"]:
                raise ValueError("Frozen evidence changed")


def execute(root) -> Any:
    """Execute the composition and retain real stage and reviewer run links."""
    import shepherd as sp

    from .tasks import check_citations

    root = Path(root).resolve()
    verify(root)
    if (root / "execution.json").exists():
        return read(root / "report.json")
    started = time.monotonic()
    with sp.workspace(model="citation-controller", root=str(root / "orchestrator")):
        run = check_citations.detailed(str(root))
    if run.trace is not None:
        write(root / "task-trace.json", run.trace.to_json())
    try:
        report = run.unwrap()
    except Exception as exc:  # noqa: BLE001 -- retain an early workflow failure as an inspectable result
        stages = [read(p) for p in (root / "stages").glob("*.json")]
        phase = (
            "extraction_failed"
            if stages and all(s["task"] == "extract_references" for s in stages)
            else "workflow_failed"
        )
        report = {
            "schema_version": "3.0",
            "reference_count": 0,
            "results": [],
            "attempts": [],
            "complete": False,
            "status": phase,
            "error": str(exc),
            "execution_counts": {phase: 1},
        }
        write(root / "report.json", report)
        (root / "report.md").write_text("# Citation report\n\n" + phase + ": " + str(exc) + "\n")
        (root / "corrections.bib").write_text("% No supported corrections: workflow failed before review.\n")
        write(root / "failure.json", {"phase": phase, "error": str(exc), "run_ref": run.ref.id})
    links = [
        {
            "batch": r["job"],
            "run_ref": r.get("run_ref"),
            "trace": str(Path(r["batch_path"]) / "trace.json"),
            "receipt": str(Path(r["batch_path"]) / "receipt.json"),
        }
        for r in report["attempts"]
    ]
    write(
        root / "task-graph.json",
        {
            "root": "check_citations",
            "stages": [
                "extract_references",
                "collect_evidence",
                "match_claims",
                "review_citation_batch",
                "build_report",
            ],
            "orchestrator_run_ref": run.ref.id,
            "stage_runs": [read(p) for p in sorted((root / "stages").glob("*.json"))],
            "review_runs": links,
        },
    )
    write(
        root / "execution.json",
        {"wall_seconds": round(time.monotonic() - started, 3), "complete": report["complete"], "run_ref": run.ref.id},
    )
    return report


def _run_frozen(root) -> Any:
    import os
    import subprocess
    import sys

    code = root / "code"
    result = subprocess.run(
        [sys.executable, "-m", "shepherd_citation_checker.cli", "run", str(root)],
        cwd=code,
        env={**os.environ, "PYTHONPATH": str(code)},
        check=False,
    )
    if result.returncode and not (root / "report.json").exists():
        raise RuntimeError(f"Citation workflow failed; see {root}")
    return read(root / "report.json")


def resume(output_dir) -> Any:
    """Resume using the run's frozen controller, even after a package upgrade."""
    root = Path(output_dir).resolve()
    verify(root)
    if Path(__file__).resolve().parent.parent == root / "code":
        return execute(root)
    return _run_frozen(root)


def check_paper(pdf_path, output_dir, *, config=None) -> Any:
    """Check every extracted reference in a PDF with the packaged five-stage workflow."""
    return _run_frozen(prepare(output_dir, pdf=pdf_path, config=config))


def check_references(references, output_dir, *, evidence=None, config=None, retrieve_initial=True) -> Any:
    """Check supplied citations; retained-evidence evaluations use retrieve_initial=False."""
    return _run_frozen(
        prepare(output_dir, references=references, evidence=evidence, config=config, retrieve_initial=retrieve_initial)
    )
