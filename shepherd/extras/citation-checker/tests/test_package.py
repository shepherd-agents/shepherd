"""Installed workflow, real stage traces and immutable evidence contracts."""

import json

import pytest
from shepherd_citation_checker import CheckerConfig
from shepherd_citation_checker._engine import hooks
from shepherd_citation_checker.api import execute, prepare, verify
from shepherd_citation_checker.models import canonical_fields
from shepherd_core.package import discover_packages


def source(tmp_path):
    path = tmp_path / "source.json"
    path.write_text(
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
    return path


def citation():
    return {
        "id": "1",
        "raw": "title: Example work\nauthors: Jane Doe\nyear: 2024\nvenue: Journal of Examples\ndoi: 10.1234/one",
    }


def test_discovered_package_exposes_composed_tasks():
    info = discover_packages()["citation_checker"]
    assert info.task_modules == ("shepherd_citation_checker.tasks",)


def test_missing_parser_extra_does_not_leave_partial_run(tmp_path, monkeypatch):
    from importlib.metadata import PackageNotFoundError

    from shepherd_citation_checker import bundle

    def missing(_):
        raise PackageNotFoundError("pdfplumber")

    monkeypatch.setattr(bundle.importlib.metadata, "distribution", missing)
    output = tmp_path / "run"
    with pytest.raises(RuntimeError, match=r"shepherd-ai\[citation-checker\]"):
        prepare(output, references=[citation()], retrieve_initial=False)
    assert not output.exists()


def test_offline_exact_match_has_real_child_traces_and_no_model_calls(tmp_path, monkeypatch):
    from shepherd_citation_checker import runtime

    monkeypatch.setattr(runtime, "launch", lambda *_: pytest.fail("Exact match must not call a model"))
    root = prepare(
        tmp_path / "run",
        references=[citation()],
        evidence={"1": [source(tmp_path)]},
        config=CheckerConfig(searches=0, fetches=0),
        retrieve_initial=False,
    )
    result = execute(root)
    assert result["complete"]
    assert result["results"][0]["metadata_status"] == "verified"
    graph = json.loads((root / "task-graph.json").read_text())
    assert {r["task"] for r in graph["stage_runs"]} == {
        "extract_references",
        "collect_evidence",
        "match_claims",
        "build_report",
    }
    assert len({r["run_ref"] for r in graph["stage_runs"]}) == 4
    assert all(r["trace"]["run_ref"]["id"] == r["run_ref"] for r in graph["stage_runs"])
    assert execute(root) == result
    path = root / "request.json"
    path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError, match="changed"):
        verify(root)


def test_conflicting_field_aliases_are_rejected():
    assert canonical_fields({"author": ["verified", []]}) == {"authors": ["verified", []]}
    with pytest.raises(ValueError, match="Conflicting"):
        canonical_fields({"author": ["verified", []], "authors": ["uncertain", []]})


def test_resume_dispatches_to_saved_code_after_package_changes(tmp_path, monkeypatch):
    from shepherd_citation_checker import api

    root = prepare(tmp_path / "run", references=[citation()], retrieve_initial=False)
    expected = {"complete": True}
    dispatched = []

    def frozen(saved):
        dispatched.append(saved)
        return expected

    monkeypatch.setattr(api, "_run_frozen", frozen)
    monkeypatch.setattr(api, "execute", lambda *_: pytest.fail("Installed controller must not resume a saved run"))
    assert api.resume(root) == expected
    assert dispatched == [root]


@pytest.mark.parametrize(
    "command",
    [
        "python3 -B -m shepherd_citation_checker._engine.documents fetch 'https://example.org'",
        "python3 -B -m shepherd_citation_checker._engine.documents fetch-many '[\"https://example.org\"]'",
    ],
)
def test_package_tool_commands_are_authorized_without_shell_expansion(tmp_path, command):
    (tmp_path / "input.json").write_text('{"references": []}')
    hooks.authorize(tmp_path, {"tool_name": "Bash", "tool_input": {"command": command}})
    with pytest.raises(ValueError, match="Bash is limited"):
        hooks.authorize(tmp_path, {"tool_name": "Bash", "tool_input": {"command": command + "; env"}})


def test_zero_search_budget_is_enforced(tmp_path):
    (tmp_path / "adjudication-limits.json").write_text('{"searches":0,"fetches":0}')
    with pytest.raises(ValueError, match="budget exhausted"):
        hooks.authorize(tmp_path, {"tool_name": "WebSearch", "tool_input": {}})


@pytest.mark.parametrize(("searches", "fetches"), [(6, 12), (0, 0)])
def test_reviewer_receives_only_the_actual_discovery_budget(tmp_path, monkeypatch, searches, fetches):
    import shutil

    from shepherd_citation_checker import runtime
    from shepherd_citation_checker.reviewer import prompt

    config = CheckerConfig(searches=searches, fetches=fetches)
    saved = prepare(tmp_path / "saved", references=[citation()], config=config, retrieve_initial=False)
    # Stage uses its containing frozen bundle, as it does in a real worker.
    monkeypatch.setattr(runtime, "__file__", str(saved / "code/shepherd_citation_checker/runtime.py"))
    staged = tmp_path / "review"
    staged.mkdir()
    packet = runtime.stage(staged, {"references": [citation()], "evidence": [], "config": config.to_dict()})
    assert packet["discovery_budget"] == {"searches": searches, "fetches": fetches}
    assert not {"retrieval_rounds_remaining", "discovery_pilot", "incremental_output"} & packet.keys()
    assert json.loads((staged / "adjudication-limits.json").read_text()) == packet["discovery_budget"]
    rendered = prompt(packet, config)
    assert f"most {searches} WebSearch calls and {fetches} fetched URLs" in rendered
    assert "Before finalizing an unresolved identity, attempt targeted discovery" in rendered
    shutil.rmtree(staged)


def test_agent_declaration_registers_from_installed_module(tmp_path):
    from click.testing import CliRunner
    from shepherd.cli import main
    from shepherd_citation_checker.agent_task import review_citations

    import shepherd as sp

    result = CliRunner().invoke(main, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    with sp.open(tmp_path) as workspace:
        registered = workspace.tasks.register(review_citations)
        assert registered is not None


def test_extraction_failure_is_reported_without_calling_an_agent(tmp_path, monkeypatch):
    from shepherd_citation_checker import runtime
    from shepherd_citation_checker._engine import extraction

    monkeypatch.setattr(runtime, "launch", lambda *_: pytest.fail("Extraction failure must not call a model"))

    def fail(_):
        raise extraction.ExtractionError("No bibliography detected")

    monkeypatch.setattr(extraction, "extract_references", fail)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-test-fixture")
    root = prepare(tmp_path / "run", pdf=pdf)
    result = execute(root)
    assert result["status"] == "extraction_failed"
    assert not result["complete"]
    assert (root / "task-trace.json").exists()


def test_authentication_failure_retains_each_citation_and_stops_dispatch(tmp_path, monkeypatch):
    from shepherd_citation_checker import runtime

    calls = []

    def expired():
        calls.append(True)
        raise RuntimeError("Subscription authentication unavailable: run claude auth login")

    monkeypatch.setattr(runtime, "auth_check", expired)
    refs = [{"id": str(i), "raw": "An unverified work. Jane Doe (2024)."} for i in range(3)]
    root = prepare(
        tmp_path / "run",
        references=refs,
        config=CheckerConfig(batch_size=1, workers=1, searches=0, fetches=0),
        retrieve_initial=False,
    )
    result = execute(root)
    assert not result["complete"]
    assert result["reference_count"] == 3
    assert {r["id"] for r in result["results"]} == {r["id"] for r in refs}
    assert len(calls) == 1
    assert result["attempts"][0]["authentication_failure"]
