"""Policy-v3 report validation: identity, field findings and provenance are separate."""

# ruff: noqa: TRY004
import copy
import json
from pathlib import Path

# ruff: noqa: T201 -- tool and CLI response protocol
from typing import Any
from urllib.parse import urlparse

from .contracts import evidence_record
from .response_health import payload_error
from .sources import catalog, normalized_title, required_fields

STATES = {"identified", "ambiguous", "not_found", "unresolved"}
KINDS = {"title_authors", "alternatives", "official_records", "identifier"}


def text(value) -> Any:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Expected nonempty explanation/text")


def unresolved(ref, reason) -> Any:
    return {
        "id": ref["id"],
        "raw": ref.get("raw", ""),
        "status": "unresolved",
        "identity": {"state": "unresolved", "explanation": reason, "supporting_fields": [], "evidence": []},
        "matched_title": None,
        "reason": reason,
        "field_checks": {},
        "evidence": [],
        "investigation": {
            "sufficient": False,
            "scope": "Execution incomplete",
            "limitations": [reason],
            "searches": [],
            "candidates_considered": [],
        },
        "metadata_status": "not_assessed",
        "corrected_citation": None,
        "correction_note": "No supported correction available",
        "suggestions": [],
        "next_queries": [],
    }


def service(url) -> Any:
    host = urlparse(url).hostname or ""
    # Registry mirrors and API/front-end hostnames are one service, not independent searches.
    for group, domains in {
        "crossref": ("crossref.org",),
        "datacite": ("datacite.org",),
        "dblp": ("dblp.org", "dblp.uni-trier.de"),
        "arxiv": ("arxiv.org",),
        "openalex": ("openalex.org",),
        "europepmc": ("europepmc.org", "ebi.ac.uk"),
        "ncbi": ("ncbi.nlm.nih.gov",),
    }.items():
        if any(host == d or host.endswith("." + d) for d in domains):
            return group
    return host.removeprefix("www.")


def validate_one(raw, ref, sources, root, rounds_remaining) -> Any:
    result = copy.deepcopy(raw)
    text(result["reason"])
    identity = result["identity"]
    state = identity["state"]
    if state not in STATES:
        raise ValueError("Unknown identity state")
    text(identity["explanation"])
    if not isinstance(identity["supporting_fields"], list):
        raise ValueError("Identity supporting_fields must be a list")
    evidence = result["evidence"]
    if not isinstance(evidence, list):
        raise ValueError("Evidence must be a list")
    allowed = {e["source_id"] for e in evidence}
    successful = set()

    def link(sid, allow_missing=False) -> Any:
        if not isinstance(sid, str) or sid not in allowed or sid not in sources:
            raise ValueError(f"Unknown or unlisted evidence ID: {sid}")
        source = sources[sid]
        record = source["record"]
        expanded = {"url": record["url"], "evidence_file": source["evidence_file"]}
        evidence_record(expanded, root, allow_missing=allow_missing)
        if payload_error(record):
            raise ValueError("Unusable response cannot support a finding")
        return expanded

    def links(ids, required=True, allow_missing=False) -> Any:
        if not isinstance(ids, list) or (required and not ids):
            raise ValueError("Finding needs a list of retained evidence IDs")
        return [link(sid, allow_missing) for sid in ids]

    for e in evidence:
        text(e["supports"])
        link(e["source_id"], allow_missing=True)
        if sources[e["source_id"]]["record"].get("http_status", 0) in range(200, 300):
            successful.add(e["source_id"])
    identity_ids = list(identity["evidence"])
    identity["evidence"] = links(identity["evidence"], required=state in {"identified", "ambiguous"})
    if state == "identified":
        text(result["matched_title"])
        if not identity["supporting_fields"]:
            raise ValueError("Identified work needs supporting citation fields")
    elif result["matched_title"] is not None:
        raise ValueError("Only identified work has a matched_title")

    checks = result["field_checks"]
    if not isinstance(checks, dict):
        raise ValueError("field_checks must be an object")
    required = set(required_fields(ref.get("raw", "")))
    if result.get("claim_policy") == "supplied-claims-1":
        from .claims import required_claims, validate_claims

        required = set(required_claims(ref.get("raw", "")))
        validate_claims(result, ref, sources)
        from .matching import complete_optional

        complete_optional(result, sources, identity_ids)
    if state == "identified" and not required <= set(checks):
        raise ValueError("Identified work needs every supplied required field check")
    for field, check in checks.items():
        if check["outcome"] not in {"verified", "error", "uncertain"}:
            raise ValueError("Unknown field outcome")
        text(check["explanation"])
        check["evidence"] = links(
            check["evidence"],
            required=check["outcome"] != "uncertain",
            allow_missing=field == "identifier" and check["outcome"] == "error",
        )

    investigation = result["investigation"]
    if type(investigation["sufficient"]) is not bool or not isinstance(investigation["limitations"], list):
        raise ValueError("Investigation needs sufficient boolean and limitations list")
    text(investigation["scope"])
    for limitation in investigation["limitations"]:
        text(limitation)
    searches = investigation["searches"]
    if not isinstance(searches, list) or not isinstance(investigation["candidates_considered"], list):
        raise ValueError("Searches and candidates must be lists")
    successful_kinds, services = set(), set()
    for search in searches:
        if search["kind"] not in KINDS:
            raise ValueError("Unknown search kind")
        text(search["query"])
        text(search["outcome"])
        ids = search["source_ids"]
        if ids and set(ids) <= successful and search["kind"] != "identifier":
            successful_kinds.add(search["kind"])
            services.update(service(sources[s]["record"]["url"]) for s in ids)
        search["evidence"] = links(ids, required=False, allow_missing=search["kind"] == "identifier")
    for candidate in investigation["candidates_considered"]:
        text(candidate["title"])
        text(candidate["explanation"])
        if candidate["disposition"] not in {"matched", "ruled_out", "unresolved"}:
            raise ValueError("Unknown candidate disposition")
        candidate["evidence"] = links(candidate["evidence"])
        if state == "not_found" and candidate["disposition"] != "ruled_out":
            raise ValueError("Unresolved or matched candidate prevents no-match finding")
    if state == "not_found" and (
        not investigation["sufficient"]
        or "title_authors" not in successful_kinds
        or not ({"alternatives", "official_records"} & successful_kinds)
        or len(services) < 2
    ):
        raise ValueError("No match needs sufficient scoped title/author and complementary searches across two services")
    if state == "unresolved" and investigation["sufficient"]:
        raise ValueError("Unresolved identity cannot claim sufficient investigation")

    outcomes = {c["outcome"] for c in checks.values()}
    result["metadata_status"] = (
        "not_assessed"
        if state != "identified"
        else "errors_found"
        if "error" in outcomes
        else "partially_verified"
        if "uncertain" in outcomes
        else "verified"
    )
    correction = result["corrected_citation"]
    if correction is not None:
        from .bibtex import render_citation

        if state != "identified" or "error" not in outcomes:
            raise ValueError("Correction requires identified work with confirmed errors")
        if normalized_title(correction["title"]) != normalized_title(result["matched_title"]):
            raise ValueError("Correction must preserve the identified work")
        correction["evidence"] = links(correction["evidence"])
        render_citation(correction, ref["id"])
    if not isinstance(result["correction_note"], str) or not isinstance(result["suggestions"], list):
        raise ValueError("Correction note and suggestions are required")
    if result["suggestions"] and state not in {"ambiguous", "not_found"}:
        raise ValueError("Suggestions are only possible intended works for ambiguous/no-match references")
    for suggestion in result["suggestions"]:
        text(suggestion["title"])
        text(suggestion["explanation"])
        suggestion["evidence"] = links(suggestion["evidence"])
    queries = result["next_queries"]
    if not isinstance(queries, list) or len(queries) > 3 or (queries and not rounds_remaining):
        raise ValueError("At most three targeted queries, only before the final review")
    for query in queries:
        text(query["purpose"])
        parsed = urlparse(query["url"])
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Follow-up query needs a public HTTP(S) URL without credentials")
    result["evidence"] = [{**link(e["source_id"], True), "supports": e["supports"]} for e in evidence]
    result["status"] = state
    return result


def validate_report(data, refs, root, rounds_remaining=0) -> Any:
    if not isinstance(data, dict) or data.get("schema_version") != "3.0" or not isinstance(data.get("results"), list):
        raise ValueError("Expected schema_version 3.0 and results list")
    rows = data["results"]
    by_id = {r["id"]: r for r in rows}
    if len(by_id) != len(rows) or not set(by_id) <= {r["id"] for r in refs}:
        raise ValueError("Duplicate or unknown reference IDs")
    sources = catalog(root)
    accepted, errors = [], {}
    for ref in refs:
        try:
            result = validate_one(by_id[ref["id"]], ref, sources, root, rounds_remaining)
        except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
            errors[ref["id"]] = f"{type(exc).__name__}: {exc}"
            result = unresolved(ref, errors[ref["id"]])
        accepted.append(result)
    return {"schema_version": "3.0", "results": accepted, "validation_errors": errors}


if __name__ == "__main__":
    root = Path.cwd()
    try:
        inputs = json.loads((root / "input.json").read_text())
        result = validate_report(
            json.loads((root / "report.json").read_text()),
            inputs["references"],
            root,
            inputs.get("retrieval_rounds_remaining", 0),
        )
        errors = result["validation_errors"]
    except (ValueError, TypeError, KeyError, OSError) as exc:
        errors = {"report": str(exc)}
    print(json.dumps({"valid": not errors, "errors": errors}))
    raise SystemExit(bool(errors))
