"""Host-owned capture of incremental, jailed reviewer output, including timeouts."""

import hashlib
import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any


def read(path) -> Any:
    """Read a JSON artifact."""
    return json.loads(path.read_text())


def write(path, data) -> Any:
    """Atomically replace a JSON artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(path)


def digest(path) -> Any:
    """Return a file content SHA-256 digest."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def output_paths(refs) -> Any:
    return {f"results/{n:03d}.json": ref["id"] for n, ref in enumerate(refs, 1)}


def copy_records(scope, destination, request) -> Any:
    """Capture complete JSON writes, never a truncated prefix or unknown ID."""
    captured = []
    for name, identifier in output_paths(request["references"]).items():
        source = scope / name
        if not source.is_file() or source.is_symlink() or (not source.resolve().is_relative_to(scope.resolve())):
            continue
        try:
            if source.stat().st_size > 200000:
                continue
            row = read(source)
            if not isinstance(row, dict) or row.get("id") != identifier:
                (destination / name).unlink(missing_ok=True)
                continue
        except (OSError, ValueError):
            (destination / name).unlink(missing_ok=True)
            continue
        write(destination / name, row)
        captured.append(identifier)
    extra = scope / "evidence/extra"
    for source in extra.rglob("*.json"):
        if source.is_symlink() or not source.resolve().is_relative_to(extra.resolve()):
            continue
        try:
            if source.stat().st_size > 8000000:
                continue
            record = read(source)
            write(destination / "evidence/extra" / source.name, record)
        except (OSError, ValueError):
            continue
    return captured


def seal(scope, protected, destination) -> Any:
    errors = []
    for name, checksum in protected.items():
        path = scope / name
        if path.is_symlink() or not path.is_file() or digest(path) != checksum:
            errors.append(name)
    result = {"protected_inputs_unchanged": not errors, "changed_or_missing": errors}
    write(destination / "integrity.json", result)
    return result


class ObservedExecution:
    """Delegate confinement unchanged; observe output before provider error handling."""

    def __init__(self, execution, job, request, protected) -> Any:
        self.execution = execution
        self.job = job
        self.request = request
        self.protected = protected

    def __getattr__(self, name) -> Any:
        return getattr(self.execution, name)

    def launch_confined(self, command, confinement) -> Any:
        scope = Path(self.execution.working_path)
        (scope / "results").mkdir(exist_ok=True)
        destination = self.job / "checkpoints"
        write(self.job / "execution-scope.json", {"path": str(scope), "protected": self.protected})
        destination.mkdir(exist_ok=True)
        for item in self.request["evidence"]:
            source = Path(item["source"])
            if digest(source) != item["sha256"]:
                raise ValueError("Initial evidence changed before capture")
            target = destination / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        stopped = threading.Event()

        def poll() -> Any:
            while not stopped.wait(0.25):
                ids = copy_records(scope, destination, self.request)
                write(destination / "progress.json", {"captured_ids": ids})

        monitor = threading.Thread(target=poll, daemon=True)
        monitor.start()
        started = time.monotonic()
        try:
            proc = self.execution.launch_confined(command, confinement)
            (self.job / "provider-stream.jsonl").write_text(proc.stdout or "")
            (self.job / "provider-stderr.txt").write_text(proc.stderr or "")
            write(
                self.job / "transport.json",
                {"returncode": proc.returncode, "seconds": round(time.monotonic() - started, 3)},
            )
            return proc
        finally:
            stopped.set()
            monitor.join(timeout=2)
            copy_records(scope, destination, self.request)
            seal(scope, self.protected, destination)


def recover_hard_stop(job, request) -> Any:
    """The worker may have died before sealing already captured results."""
    pointer = job / "execution-scope.json"
    if pointer.exists():
        info = read(pointer)
        scope = Path(info["path"])
        copy_records(scope, job / "checkpoints", request)
        seal(scope, info["protected"], job / "checkpoints")


def finalize(job, request, receipt) -> Any:
    from .assessment import expand, guard_identity
    from .sources import catalog
    from .validation import validate_report

    root = job / "checkpoints"
    integrity = root / "integrity.json"
    if not integrity.exists() or not read(integrity)["protected_inputs_unchanged"]:
        receipt["checkpoint_error"] = "No verified unchanged-input seal; captured drafts are not accepted"
        return receipt
    rows = []
    for name, identifier in output_paths(request["references"]).items():
        path = root / name
        if path.exists():
            row = read(path)
            if row.get("id") == identifier:
                rows.append(row)
    raw = {"schema_version": "compact-3", "results": rows}
    write(root / "compact-model-report.json", raw)
    expanded = expand(raw)
    write(root / "model-report.json", expanded)
    guarded, issues = guard_identity(expanded, catalog(root))
    write(root / "adjudicated-report.json", guarded)
    validated = validate_report(guarded, request["references"], root)
    write(root / "report.json", validated)
    captured = {r["id"] for r in rows}
    states = {
        r["id"]: "completed"
        if r["id"] in captured and r["id"] not in validated["validation_errors"]
        else "validation_failed"
        if r["id"] in captured
        else "not_completed"
        for r in request["references"]
    }
    receipt.update(
        results=validated["results"],
        validation_errors=validated["validation_errors"],
        reference_execution=states,
        checkpoint_directory=str(root),
        model_contract_issues=issues,
    )
    if all(v == "completed" for v in states.values()):
        receipt["status"] = "completed"
    elif any(v == "completed" for v in states.values()):
        receipt["status"] = "partial"
    else:
        receipt["status"] = "failed"
    return receipt
