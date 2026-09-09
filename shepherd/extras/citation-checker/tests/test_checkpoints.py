"""Provider alarms, corrupt writes and mutated evidence preserve valid neighbors."""

import json
import subprocess
from types import SimpleNamespace

from shepherd_citation_checker import storage
from shepherd_citation_checker._engine.batching import pack
from shepherd_citation_checker._engine.checkpoints import ObservedExecution, copy_records, finalize, recover_hard_stop
from shepherd_citation_checker._engine.sources import source_id


def item():
    return {
        "DOI": "10.1234/one",
        "title": ["A distinctive study"],
        "author": [{"given": "Alice Beth", "family": "Smith"}],
        "published": {"date-parts": [[2024]]},
        "container-title": ["Science"],
        "page": "1-4",
    }


def reference():
    return {
        "id": "1",
        "raw": "title: A distinctive study\nauthors: Alice Beth Smith\nyear: 2024\nvenue: Science\ndoi: 10.1234/one\npages: 1-4",
        "prefetched": [],
    }


def record(items=None):
    return {
        "url": "https://api.crossref.org/works?query.title=study",
        "http_status": 200,
        "body": json.dumps({"status": "ok", "message": {"items": items or [item()]}}),
    }


def fixture(tmp_path):
    source = record()
    storage.write(tmp_path / "source.json", source)
    refs = [{**reference(), "id": str(i)} for i in (1, 2)]
    request = {
        "references": refs,
        "phase": "0-review",
        "incremental_batch": True,
        "provider_budget_seconds": 0.1,
        "worker_budget_seconds": 0.4,
        "evidence": [
            {
                "source": str(tmp_path / "source.json"),
                "path": "evidence/source.json",
                "sha256": storage.digest(tmp_path / "source.json"),
            }
        ],
    }
    job = tmp_path / "job"
    storage.write(job / "request.json", request)
    scope = tmp_path / "scope"
    storage.write(scope / "input.json", {"references": refs})
    protected = {"input.json": storage.digest(scope / "input.json")}
    sid = source_id(source)
    row = {
        "id": "1",
        "state": "identified",
        "title": "A distinctive study",
        "reason": "All supplied fields agree.",
        "identity_fields": ["title", "authors"],
        "sources": [sid],
        "fields": {f: ["verified", [sid]] for f in ("title", "authors", "year", "venue", "identifier", "pages")},
    }
    return job, scope, request, protected, row


def test_alarm_preserves_stdout_and_one_completed_neighbor(tmp_path):
    job, scope, request, protected, row = fixture(tmp_path)
    command, confinement = ["unchanged-command"], object()
    calls = []
    stdout = '{"type":"assistant","message":{"model":"test-opus"}}\n'

    def launch(actual_command, actual_confinement):
        calls.append((actual_command, actual_confinement))
        storage.write(scope / "results/001.json", row)
        (scope / "results/002.json").write_text('{"id":')
        return subprocess.CompletedProcess(command, -14, stdout, "alarm fired")

    observed = ObservedExecution(SimpleNamespace(working_path=scope, launch_confined=launch), job, request, protected)
    proc = observed.launch_confined(command, confinement)
    assert proc.returncode == -14
    assert calls == [(command, confinement)]
    assert (job / "provider-stream.jsonl").read_text() == stdout
    assert storage.read(job / "transport.json")["returncode"] == -14
    receipt = finalize(
        job,
        request,
        {
            "job": job.name,
            "ids": ["1", "2"],
            "status": "failed",
            "runtime_status": "failed",
            "batch_path": str(job),
            "error": "Provider budget exceeded",
        },
    )
    assert receipt["status"] == "partial"
    assert receipt["reference_execution"] == {"1": "completed", "2": "not_completed"}


def test_protected_mutation_invalidates_captured_results(tmp_path):
    job, scope, request, protected, row = fixture(tmp_path)

    def launch(*_):
        storage.write(scope / "results/001.json", row)
        (scope / "input.json").write_text("changed")
        return subprocess.CompletedProcess([], 0, "", "")

    ObservedExecution(
        SimpleNamespace(working_path=scope, launch_confined=launch), job, request, protected
    ).launch_confined([], None)
    result = finalize(job, request, {"status": "failed"})
    assert "results" not in result
    assert "checkpoint_error" in result


def test_malformed_completed_row_does_not_discard_valid_neighbor(tmp_path):
    job, scope, request, protected, row = fixture(tmp_path)
    storage.write(scope / "results/001.json", row)
    storage.write(scope / "results/002.json", {"id": "2", "state": "identified"})
    storage.write(job / "checkpoints/evidence/source.json", record())
    storage.write(job / "execution-scope.json", {"path": str(scope), "protected": protected})
    recover_hard_stop(job, request)
    result = finalize(job, request, {"status": "failed"})
    assert result["reference_execution"] == {"1": "completed", "2": "validation_failed"}
    # A later invalid file must not silently reuse an earlier valid checkpoint.
    (scope / "results/001.json").write_text("{")
    copy_records(scope, job / "checkpoints", request)
    assert not (job / "checkpoints/results/001.json").exists()


def test_workload_balancing_is_reproducible_and_respects_limits():
    items = [{"id": str(i), "input_tokens": 500 + i * 200, "output_tokens": 500} for i in range(15)]
    a = pack(items, max_citations=4, max_input_tokens=7000, max_output_tokens=2000)
    assert a == pack(list(reversed(items)), max_citations=4, max_input_tokens=7000, max_output_tokens=2000)
    assert sorted(i for b in a for i in b["ids"]) == sorted(i["id"] for i in items)
    assert all(len(b["ids"]) <= 4 and b["input_tokens"] <= 7000 and b["output_tokens"] <= 2000 for b in a)


def test_hard_worker_deadline_recovers_completed_neighbors(tmp_path, monkeypatch):
    from shepherd_citation_checker import runtime

    job, scope, request, protected, row = fixture(tmp_path)
    request["config"] = {"worker_seconds": 0.4}
    storage.write(job / "request.json", request)
    storage.write(scope / "results/001.json", row)
    storage.write(job / "checkpoints/evidence/source.json", record())
    storage.write(job / "execution-scope.json", {"path": str(scope), "protected": protected})
    # Exercise the real process deadline without starting a provider or making HTTP requests.
    code = tmp_path / "code"
    module = code / "shepherd_citation_checker"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text("")
    (module / "runtime.py").write_text("import time\ntime.sleep(60)\n")
    monkeypatch.setattr(runtime, "auth_check", lambda: None)
    receipt = runtime.launch(job, code)
    assert receipt["status"] == "partial"
    assert receipt["reference_execution"] == {"1": "completed", "2": "not_completed"}
    assert "without a receipt" in receipt["error"]
    assert receipt["elapsed_seconds"] < 3
