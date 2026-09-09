"""Regression checks for absent fields, supported contradictions and surface forms."""

# ruff: noqa: RUF001
import copy
import json

import pytest
from shepherd_citation_checker import storage as runner
from shepherd_citation_checker._engine.assessment import expand
from shepherd_citation_checker._engine.claims import bare_url, comparison_key, required_claims
from shepherd_citation_checker._engine.sources import source_id
from shepherd_citation_checker._engine.summary import assessment
from shepherd_citation_checker._engine.validation import validate_report


def item():
    return {
        "DOI": "10.1234/one",
        "title": ["A distinctive study"],
        "author": [{"given": "Alice Beth", "family": "Smith"}],
        "published": {"date-parts": [[2024]]},
        "container-title": ["Science"],
        "page": "1-4",
    }


def record(items=None):
    return {
        "url": "https://api.crossref.org/works?query.title=study",
        "http_status": 200,
        "body": json.dumps({"status": "ok", "message": {"items": items or [item()]}}),
    }


def sample(tmp_path):
    source = record()
    runner.write(tmp_path / "evidence/source.json", source)
    sid = source_id(source)
    ref = {"id": "1", "raw": "A distinctive study, 2024. doi: 10.1234/one"}
    compact = {
        "id": "1",
        "state": "identified",
        "title": "A distinctive study",
        "identity_fields": ["title", "identifier"],
        "sources": [sid],
        "reason": "Supplied claims agree.",
        "fields": {f: ["verified", [sid]] for f in ["title", "year", "identifier"]},
        "not_supplied": ["venue"],
    }
    return ref, compact, sid


def check(tmp_path, ref, compact):
    return validate_report(expand({"schema_version": "compact-3", "results": [compact]}), [ref], tmp_path)


def test_absent_venue_does_not_create_warning(tmp_path):
    ref, compact, _sid = sample(tmp_path)
    result = check(tmp_path, ref, compact)
    assert not result["validation_errors"]
    assert result["results"][0]["metadata_status"] == "verified"
    assert assessment(result["results"][0])["attention"] == "none"
    compact["not_supplied"] = []
    compact["fields"]["venue"] = ["uncertain", [], "No conference given in arXiv"]
    assert "supplied citation span" in check(tmp_path, ref, compact)["validation_errors"]["1"]


def test_uncertainty_must_point_to_real_supplied_claim(tmp_path):
    ref, compact, _sid = sample(tmp_path)
    compact["fields"]["year"] = ["uncertain", [], "Edition date unavailable"]
    compact["claims"] = {"year": "2024"}
    result = check(tmp_path, ref, compact)
    assert not result["validation_errors"]
    assert assessment(result["results"][0])["attention"] == "verification_gap"
    compact["claims"]["year"] = "2025"
    assert "occur in the citation" in check(tmp_path, ref, compact)["validation_errors"]["1"]


def test_wrapped_url_claim_preserves_the_actual_identifier(tmp_path):
    ref, compact, _sid = sample(tmp_path)
    ref["raw"] += " URL https:\n//openreview.net/forum?id=real123 ."
    compact["fields"]["url"] = ["uncertain", [], "Page blocked"]
    compact["claims"] = {"url": "https://openreview.net/forum?id=real123"}
    assert not check(tmp_path, ref, compact)["validation_errors"]
    compact["claims"]["url"] = "https://openreview.net/forum?id=wrong456"
    assert check(tmp_path, ref, compact)["validation_errors"]


def test_error_requires_actual_source_excerpt_and_applicability(tmp_path):
    ref, compact, sid = sample(tmp_path)
    ref["raw"] = ref["raw"].replace("distinctive", "fabricated")
    compact["fields"]["title"] = ["error", [sid], "DOI identifies a different title."]
    compact["claims"] = {"title": "A fabricated study"}
    compact["contradictions"] = {
        "title": {
            "source_id": sid,
            "quote": "A distinctive study",
            "same_work": "Exact supplied DOI identifies this record.",
        }
    }
    result = check(tmp_path, ref, compact)
    assert not result["validation_errors"]
    assert assessment(result["results"][0])["attention"] == "issue"
    bad = copy.deepcopy(compact)
    bad["contradictions"]["title"]["quote"] = "An invented source excerpt"
    assert "not found in retained source" in check(tmp_path, ref, bad)["validation_errors"]["1"]
    compact["contradictions"]["title"]["same_work"] = ""
    assert "applicability" in check(tmp_path, ref, compact)["validation_errors"]["1"]


@pytest.mark.parametrize("quote", ["[[2023,6]]", "[[ 2023, 6 ]]", '"date-parts": [ [ 2023, 6 ] ]'])
def test_numeric_json_excerpt_is_retained_evidence_not_an_invented_quote(tmp_path, quote):
    ref, compact, _sid = sample(tmp_path)
    source = record()
    body = json.loads(source["body"])
    body["message"]["published-print"] = {"date-parts": [[2023, 6]]}
    source["body"] = json.dumps(body, separators=(",", ":"))
    runner.write(tmp_path / "evidence/source.json", source)
    sid = source_id(source)
    compact["sources"] = [sid]
    compact["fields"] = {f: ["verified", [sid]] for f in ["title", "year", "identifier"]}
    compact["fields"]["year"] = ["error", [sid], "Published-print date is June 2023, not the cited 2024."]
    compact["claims"] = {"year": "2024"}
    compact["contradictions"] = {
        "year": {"source_id": sid, "quote": quote, "same_work": "Published-print date of the exact cited DOI."}
    }
    assert not check(tmp_path, ref, compact)["validation_errors"]
    compact["contradictions"]["year"]["quote"] = "[[2022,6]]"
    assert "not found" in check(tmp_path, ref, compact)["validation_errors"]["1"]


def test_readable_bare_url_does_not_require_unsupplied_title(tmp_path):
    ref, compact, sid = sample(tmp_path)
    ref["raw"] = "https://example.org/study. URL https://example.org/study ."
    assert bare_url(ref["raw"])
    compact["fields"] = {"url": ["verified", [sid]]}
    compact["not_supplied"] = ["title", "author", "year", "venue"]
    compact["identity_fields"] = ["url"]
    result = check(tmp_path, ref, compact)
    assert not result["validation_errors"]
    assert result["results"][0]["metadata_status"] == "verified"


def test_work_type_edition_and_wrapped_ids_cannot_disappear():
    assert "work_type" in required_claims("Company. A paper, 2025. White Paper.")
    assert "edition" in required_claims("A book, 1st edition, 1998.")
    assert "identifier" in required_claims("A paper, 2025. URL https://arxiv. org/abs/2512.20856 .")


def test_discontinuous_source_fragments_must_each_exist(tmp_path):
    ref, compact, sid = sample(tmp_path)
    ref["raw"] = ref["raw"].replace("distinctive", "fabricated")
    compact["fields"]["title"] = ["error", [sid], "Different title."]
    compact["claims"] = {"title": "A fabricated study"}
    compact["contradictions"] = {
        "title": {
            "source_id": sid,
            "quote": ["A distinctive study", "10.1234/one"],
            "same_work": "Title and DOI are separate metadata entries in this record.",
        }
    }
    assert not check(tmp_path, ref, compact)["validation_errors"]
    compact["contradictions"]["title"]["quote"].append("Invented fragment")
    assert check(tmp_path, ref, compact)["validation_errors"]


@pytest.mark.parametrize(
    ("field", "left", "right"),
    [
        ("title", "A\n distinct study", "A distinct study"),
        ("title", "G\u006f\u0308del", "Gödel"),
        ("identifier", "https://doi.org/10.1234/ONE", "doi:10.1234/one"),
        ("pages", "pp. 1–32", "1-32"),
    ],
)
def test_harmless_surface_forms(field, left, right):
    assert comparison_key(field, left) == comparison_key(field, right)


@pytest.mark.parametrize(
    ("field", "left", "right"),
    [
        ("title", "OpenHands V1", "The OpenHands Software Agent SDK"),
        ("authors", "Wang, Pan, Hui", "Wang, Rosenberg, Michelini"),
        ("identifier", "10.1234/one", "10.1234/two"),
        ("pages", "51:1–51:32", "1-32"),
        ("year", "1999", "1998"),
    ],
)
def test_substantive_differences_survive_normalization(field, left, right):
    assert comparison_key(field, left) != comparison_key(field, right)
