"""The public five-stage citation-checking composition."""

import concurrent.futures
import contextvars
import json
import time
from pathlib import Path
from typing import Any

import shepherd as sp

from .storage import digest, read, write


@sp.task
def extract_references(run_dir: str) -> str:
    """Extract PDF bibliography text, or validate supplied frozen citations."""
    from ._engine.extraction import extract_references as extract

    root = Path(run_dir)
    request = read(root / "request.json")
    if request["pdf"]:
        refs, pages, details = extract(root / "input/source.pdf")
        write(root / "pages.json", pages)
        write(root / "extraction.json", details)
    else:
        refs = request["references"]
    if not refs or len({r["id"] for r in refs}) != len(refs):
        raise ValueError("Expected nonempty citations with unique string IDs")
    if any(not isinstance(r.get("id"), str) or not isinstance(r.get("raw"), str) or not r["raw"].strip() for r in refs):
        raise ValueError("Every citation needs a string ID and nonempty raw text")
    write(root / "citations.json", {"references": refs})
    return str(root / "citations.json")


@sp.task
def collect_evidence(citations_path: str) -> str:
    """Retain original sources and collect initial identifier/candidate evidence."""
    from .collection import collect

    return collect(Path(citations_path))


@sp.task
def match_claims(evidence_path: str) -> str:
    """Accept complete exact matches and batch the remaining citations for review."""
    from ._engine.batching import estimate, pack
    from ._engine.matching import match
    from ._engine.sources import catalog
    from ._engine.validation import validate_report

    root = Path(evidence_path).parent
    config = read(root / "request.json")["config"]
    refs = read(evidence_path)["references"]
    sources = catalog(root)
    by_path = {s["evidence_file"]: sid for sid, s in sources.items()}
    decisions, pending, reasons = [], [], []
    for ref in refs:
        own_ids = {by_path[e["evidence_file"]] for e in ref["prefetched"]}
        own = {sid: sources[sid] for sid in own_ids}
        decision, reason = match(ref, own)
        if decision:
            checked = validate_report({"schema_version": "3.0", "results": [decision]}, [ref], root)
            if checked["validation_errors"]:
                decision, reason = None, "Exact match failed shared validation"
            else:
                decision = checked["results"][0]
        reasons.append({"id": ref["id"], "route": "deterministic" if decision else "opus", "reason": reason})
        if decision:
            decisions.append(
                {**decision, "raw": ref["raw"], "execution_status": "completed", "review_method": "deterministic"}
            )
        else:
            pending.append(estimate(ref, own))
    batches = (
        pack(
            pending,
            max_citations=config["batch_size"],
            max_input_tokens=config["batch_input_tokens"],
            max_output_tokens=config["batch_output_tokens"],
        )
        if pending
        else []
    )
    paths = []
    for index, batch in enumerate(batches, 1):
        batch_refs = [next(r for r in refs if r["id"] == identifier) for identifier in batch["ids"]]
        evidence = {}
        for ref in batch_refs:
            for e in ref["prefetched"]:
                path = e["evidence_file"]
                evidence[path] = {"path": path, "source": str(root / path), "sha256": digest(root / path)}
        job = root / "jobs" / f"batch-{index:03d}"
        write(
            job / "request.json",
            {
                "references": batch_refs,
                "evidence": list(evidence.values()),
                "config": config,
                "claim_policy": "supplied-claims-1",
            },
        )
        paths.append(str(job))
    write(root / "routing.json", {"rows": reasons, "batches": paths, "batch_plan": batches})
    write(root / "deterministic.json", {"results": decisions})
    return str(root / "routing.json")


@sp.task
def review_citation_batch(job_path: str) -> dict:
    """Run one bounded Opus session, retaining its linked Shepherd run and outputs."""
    from .runtime import launch

    job = Path(job_path)
    result = launch(job, job.parents[1] / "code")
    sp.emit_artifact(name=f"{job.name}-receipt.json", kind="json", content=json.dumps(result))
    return result


@sp.task
def build_report(routing_path: str) -> dict:
    """Validate retained decisions and emit findings, explanations and corrections."""
    from .reporting import build

    return build(Path(routing_path).parent)


def _stage(root, task, *args: Any) -> Any:
    started = time.monotonic()
    run = task.detailed(*args)
    artifact = {
        "task": task.__name__,
        "run_ref": run.ref.id,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "trace": run.trace.to_json() if run.trace is not None else None,
    }
    path = Path(root) / "stages" / (task.__name__ + "-" + run.ref.id + ".json")
    write(path, artifact)
    sp.emit_artifact(name=path.name, kind="json", content=json.dumps(artifact))
    return run.unwrap()


def _review_batches(run_dir: str, routing: str) -> None:
    """Bound concurrent batch sessions and stop dispatch on authentication failure."""
    config = read(Path(run_dir) / "request.json")["config"]
    jobs = iter(read(routing)["batches"])
    with concurrent.futures.ThreadPoolExecutor(max_workers=config["workers"]) as pool:
        active = {}
        failed_auth = False

        def fill() -> Any:
            while not failed_auth and len(active) < config["workers"]:
                job = next(jobs, None)
                if job is None:
                    break
                context = contextvars.copy_context()
                active[pool.submit(context.run, _stage, run_dir, review_citation_batch, job)] = job

        fill()
        while active:
            done, _ = concurrent.futures.wait(active, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                active.pop(future)
                receipt = future.result()
                failed_auth |= bool(receipt.get("authentication_failure"))
            fill()


@sp.task
def check_citations(run_dir: str) -> dict:
    """Check bibliography identity and metadata through five explicit stages."""
    citations = _stage(run_dir, extract_references, run_dir)
    evidence = _stage(run_dir, collect_evidence, citations)
    routing = _stage(run_dir, match_claims, evidence)
    _review_batches(run_dir, routing)
    report = _stage(run_dir, build_report, routing)
    for name, kind in [("report.json", "json"), ("report.md", "markdown"), ("corrections.bib", "text")]:
        sp.emit_artifact(name=name, kind=kind, content=(Path(run_dir) / name).read_text())
    return report
