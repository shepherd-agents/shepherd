"""Exact matching escalates conflicting and incompletely parsed citations."""

import pytest
from shepherd_citation_checker._engine.matching import match
from shepherd_citation_checker._engine.sources import catalog, source_id
from shepherd_citation_checker._engine.validation import validate_report
from shepherd_citation_checker.storage import write


def arxiv():
    return {
        "url": "https://arxiv.org/abs/2401.12345",
        "http_status": 200,
        "body": '<html><meta name="citation_title" content="A reliable multi-agent system">'
        '<meta name="citation_author" content="Smith, Alice B">'
        '<meta name="citation_author" content="Opsahl-Ong, Krista">'
        '<meta name="citation_date" content="2024/01/02">'
        '<meta name="citation_arxiv_id" content="2401.12345">cs.AI</html>',
    }


def ref():
    return {
        "id": "1",
        "raw": "Alice B. Smith and Krista Opsahl-\nOng. A reliable multi-\nagent system, January 2024. URL https:\n//arxiv.\norg/abs/2401.\n12345 . arXiv:2401.12345 [cs].",
    }


def check(reference, records):
    return match(reference, {source_id(r): {"record": r} for r in records})[0]


def test_arxiv_full_comparison_validates(tmp_path):
    write(tmp_path / "evidence/arxiv.json", arxiv())
    row, _ = match(ref(), catalog(tmp_path))
    assert row
    result = validate_report({"schema_version": "3.0", "results": [row]}, [ref()], tmp_path)
    assert not result["validation_errors"]
    assert result["results"][0]["metadata_status"] == "verified"


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("Alice", "Adam"),
        ("Alice B.", "A. B."),
        (" and Krista Opsahl-\nOng", ""),
        ("system", "different system"),
        ("January", "February"),
        ("2024", "2023"),
        ("arXiv:2401.12345", "arXiv:2401.99999"),
        ("[cs]", "[math]"),
        ("12345 .", "12345v2 ."),
        (". URL", ". Nature. URL"),
    ],
)
def test_arxiv_never_accepts_conflicts_or_partial_parse(before, after):
    r = ref()
    r["raw"] = r["raw"].replace(before, after)
    assert check(r, [arxiv()]) is None


def test_arxiv_unparsed_suffix_untrusted_host_and_versions_escalate():
    r = ref()
    r["raw"] += " White paper."
    assert check(r, [arxiv()]) is None
    source = arxiv()
    source["url"] = "https://example.org/abs/2401.12345"
    assert check(ref(), [source]) is None
    source = arxiv()
    source["body"] = source["body"].replace("Alice B", "Adam B")
    assert check(ref(), [arxiv(), source]) is None
    source = arxiv()
    source["http_status"] = 429
    assert check(ref(), [source]) is None
