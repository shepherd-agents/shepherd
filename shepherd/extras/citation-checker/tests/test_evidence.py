"""Evidence projections and completion must preserve ambiguity and source scope."""

import copy
import json

import pytest
from shepherd_citation_checker._engine.claims import explicit_annotation, required_claims, validate_claims
from shepherd_citation_checker._engine.documents import materialize
from shepherd_citation_checker._engine.matching import complete_optional
from shepherd_citation_checker._engine.notes import bibliographic_notes, embedded_markdown
from shepherd_citation_checker._engine.sources import compact
from shepherd_citation_checker._engine.sources import preview_packet as packet_view


def registry(location="Paris, France"):
    return {
        "url": "https://api.crossref.org/works/10.1234/example",
        "http_status": 200,
        "body": json.dumps(
            {
                "message": {
                    "title": ["Example work"],
                    "DOI": "10.1234/example",
                    "event": {"location": location, "start": {"date-parts": [[2020, 6]]}},
                    "editor": [{"given": "Jane", "family": "Doe"}],
                    "ISSN": ["1234-5678"],
                    "volume": "30",
                }
            }
        ),
    }


def result():
    return {
        "identity": {"state": "identified"},
        "matched_title": "Example work",
        "claim_policy": "supplied-claims-1",
        "claim_spans": {"location": "Paris, France", "volume": "30"},
        "field_checks": {
            "title": {"outcome": "verified"},
            "authors": {"outcome": "verified"},
            "location": {"outcome": "uncertain"},
            "volume": {"outcome": "uncertain"},
        },
    }


def test_crossref_fields_survive_both_projections():
    record = registry()
    projected = packet_view({"sources": [compact(record)]})["sources"][0]["candidates"][0]
    assert projected["event"]["location"] == "Paris, France"
    assert projected["editor"] == [{"given": "Jane", "family": "Doe"}]
    assert projected["ISSN"] == ["1234-5678"]


def test_exact_completion_has_provenance_and_does_not_mutate_sources():
    sources = {"s": {"record": registry()}}
    before = copy.deepcopy(sources)
    row = result()
    complete_optional(row, sources, ["s"])
    assert row["field_checks"]["location"]["outcome"] == "verified"
    assert row["deterministic_completions"][0]["source_field"] == "event.location"
    assert sources == before


@pytest.mark.parametrize("change", ["conflict", "unrelated", "authors", "error", "failed", "multiple"])
def test_completion_does_not_hide_gaps_or_overwrite_errors(change):
    row, sources = result(), {"s": {"record": registry()}}
    if change == "conflict":
        sources["other"] = {"record": registry("Rome, Italy")}
    elif change == "unrelated":
        row["matched_title"] = "Different work"
    elif change == "authors":
        row["field_checks"]["authors"]["outcome"] = "uncertain"
    elif change == "error":
        row["field_checks"]["location"]["outcome"] = "error"
    elif change == "failed":
        sources["s"]["record"]["http_status"] = 429
    elif change == "multiple":
        body = json.loads(sources["s"]["record"]["body"])
        sources["s"]["record"]["body"] = json.dumps({"message": {"items": [body["message"], body["message"]]}})
    previous = row["field_checks"]["location"]["outcome"]
    complete_optional(row, sources, list(sources))
    assert row["field_checks"]["location"]["outcome"] == previous


def test_title_words_and_absent_versions_are_not_annotations():
    assert "work_type" not in required_claims("A. Author. Qwen3 Technical Report. 2025.")
    assert not explicit_annotation("ANSI/NISO Z39.96-2024: JATS: Journal Article Tag Suite (2024)", "version")
    assert explicit_annotation("A work. Technical Report.", "work_type")
    assert explicit_annotation("A work, version 1.4", "version")
    assert explicit_annotation("A book, 1st edition", "edition")


@pytest.mark.parametrize(
    ("field", "raw", "claim"),
    [
        ("work_type", "A Technical Report. arXiv preprint arXiv:1234.56789.", "A Technical Report"),
        ("version", "JATS: Journal Article Tag Suite (2024)", "JATS: Journal Article Tag Suite (2024)"),
        ("edition", "A book. 2003. Springer.", "2003. Springer"),
    ],
)
def test_validator_rejects_semantically_unrelated_claim_spans(field, raw, claim):
    row = {"field_checks": {field: {"outcome": "uncertain"}}, "claim_spans": {field: claim}, "not_supplied": []}
    with pytest.raises(ValueError, match=r"Work-type claim|No explicit supplied"):
        validate_claims(row, {"raw": raw}, {})


def test_valid_preprint_annotation_does_not_add_a_new_required_check():
    raw = "A work. arXiv preprint arXiv:1234.56789, 2025."
    assert explicit_annotation(raw, "work_type")
    assert "work_type" not in required_claims(raw)
    validate_claims(
        {"field_checks": {"work_type": {"outcome": "verified"}}, "claim_spans": {}, "not_supplied": []},
        {"raw": raw},
        {},
    )


def test_completion_does_not_promote_event_month_or_front_matter_authors():
    row = result()
    for field, claim in [("publication_month", "June"), ("editors", "Jane Doe")]:
        row["field_checks"][field] = {"outcome": "uncertain"}
        row["claim_spans"][field] = claim
    complete_optional(row, {"s": {"record": registry()}}, ["s"])
    assert row["field_checks"]["publication_month"]["outcome"] == "uncertain"
    assert row["field_checks"]["editors"]["outcome"] == "uncertain"


def test_malformed_optional_registry_values_do_not_fail_the_review():
    record = registry()
    body = json.loads(record["body"])
    body["message"].update(event=None, DOI=None)
    record["body"] = json.dumps(body)
    row = result()
    complete_optional(row, {"s": {"record": record}}, ["s"])
    assert row["field_checks"]["location"]["outcome"] == "uncertain"


def test_acl_labeled_fields_exclude_navigation_and_keep_provenance():
    record = {
        "url": "https://aclanthology.org/example/",
        "http_status": 200,
        "body": "<nav>London</nav><dl><dt>Address:</dt><dd>Paris, France</dd><dt>Editors:</dt><dd>Jane Doe</dd></dl>",
    }
    notes = bibliographic_notes(record)
    assert [s["text"] for s in notes["sections"]] == ["Paris, France", "Jane Doe"]
    assert notes["sections"][0]["locator"].startswith("dl")


def test_github_raw_lines_are_materialized_without_inventing_authorship(tmp_path):
    markdown = "**Model Release Date** April 18, 2024\n```bibtex\n@misc{x, author={AI@Meta}, year={2024}}\n```"
    body = (
        '<script type="application/json">'
        + json.dumps({"payload": {"blob": {"rawLines": markdown.splitlines()}}})
        + "</script>"
    )
    record = {
        "url": "https://github.com/org/repo/blob/main/MODEL_CARD.md",
        "http_status": 200,
        "body": body,
        "content_type": "text/html",
    }
    (tmp_path / "evidence").mkdir()
    (tmp_path / "evidence/page.json").write_text(json.dumps(record))
    indexed = materialize(tmp_path)[0]
    assert (tmp_path / indexed["text"]).read_text() == markdown
    notes = bibliographic_notes(record)
    assert {s["field"] for s in notes["sections"]} == {"suggested_citation", "release_date"}
    assert embedded_markdown({**record, "url": record["url"].replace("github.com", "github.com.evil.example")}) == ""
    assert embedded_markdown({**record, "body": body + body.replace("AI@Meta", "SomeoneElse")}) == ""
