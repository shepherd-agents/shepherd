"""Compact batch presentation and lossless expansion of model decisions."""

import copy
import json
from typing import Any

from ..models import canonical_fields
from .validation import unresolved


def expand(data) -> Any:
    if data.get("schema_version") != "compact-3" or not isinstance(data.get("results"), list):
        raise ValueError("Expected compact results")
    rows = []
    for original in data["results"]:
        if not isinstance(original, dict) or not isinstance(original.get("id"), str):
            raise TypeError("Compact result needs an explicit string ID")
        try:
            row = unresolved({"id": original["id"]}, original["reason"])
            checks = {}
            for field, value in canonical_fields(original["fields"]).items():
                if not isinstance(value, list) or len(value) not in (2, 3):
                    raise ValueError("Field requires outcome, source IDs and explanation")
                if len(value) == 2 and value[0] != "verified":
                    raise ValueError("Error/uncertain finding needs an explanation")
                checks[field] = {
                    "outcome": value[0],
                    "evidence": value[1],
                    "explanation": value[2] if len(value) == 3 else "Matches the retained evidence.",
                }
            identity_sources = original["sources"]
            sources = list(identity_sources)
            for check in checks.values():
                sources.extend(check["evidence"])
            correction = original.get("correction")
            enrichment = None
            if correction and not any(c["outcome"] == "error" for c in checks.values()):
                # Preserve optional enrichment separately; never manufacture an
                # error merely to make a proposed complete citation admissible.
                enrichment, correction = correction, None
            if correction:
                sources.extend(correction["evidence"])
            investigation = original.get(
                "investigation",
                {
                    "sufficient": original["state"] == "identified",
                    "scope": "Comparison with retained evidence in this batch",
                    "limitations": original.get("limitations", []),
                    "searches": [],
                    "candidates_considered": [],
                },
            )
            for search in investigation["searches"]:
                sources.extend(search["source_ids"])
            for candidate in investigation["candidates_considered"]:
                sources.extend(candidate["evidence"])
            row.update(
                identity={
                    "state": original["state"],
                    "explanation": original["reason"],
                    "evidence": identity_sources,
                    "supporting_fields": original["identity_fields"],
                },
                matched_title=original["title"],
                field_checks=checks,
                evidence=[{"source_id": sid, "supports": original["reason"]} for sid in dict.fromkeys(sources)],
                investigation=investigation,
                corrected_citation=correction,
                correction_note=original.get("correction_note", "No complete correction supplied."),
            )
            if enrichment:
                row["unvalidated_enrichment"] = enrichment
            if original.get("enrichment_note"):
                row["enrichment_note"] = original["enrichment_note"]
            if data["schema_version"] == "compact-3":
                row.update(
                    claim_policy="supplied-claims-1",
                    claim_spans=canonical_fields(original.get("claims", {})),
                    not_supplied=original.get("not_supplied", []),
                    contradictions=canonical_fields(original.get("contradictions", {})),
                )
        except (KeyError, TypeError, ValueError, AttributeError):
            # Shared validator marks this row incomplete, keeping valid neighbors.
            row = {"id": original["id"], "compact_expansion_error": "Malformed compact result"}
        rows.append(row)
    return {"schema_version": "3.0", "results": rows, "producer": "host-expanded compact Opus decisions"}


def guard_identity(report, sources) -> Any:
    """Downgrade unsupported identity claims; never promote missing evidence.

    This only handles known retained sources and purely uncertain field findings.
    Unknown source IDs or unsupported verified/error findings remain validation
    failures. Original model output is retained separately by the caller.
    """
    from .response_health import usable

    guarded = copy.deepcopy(report)
    issues = {}
    for row in guarded["results"]:
        try:
            identity = row["identity"]
            ids = identity["evidence"]
            if not isinstance(ids, list) or any(sid not in sources for sid in ids):
                continue
            checks = row["field_checks"]
            if any(v["outcome"] != "uncertain" for v in checks.values()):
                continue
            all_ids = {e["source_id"] for e in row["evidence"]} | set(ids)
            all_ids.update(sid for check in checks.values() for sid in check["evidence"])
            if any(sid not in sources for sid in all_ids):
                continue
            usable_ids = {sid for sid in all_ids if usable(sources[sid]["record"])}
            missing_identity = identity["state"] == "identified" and (
                not row["matched_title"] or not set(ids) & usable_ids
            )
            failed_support = identity["state"] == "unresolved" and all_ids - usable_ids
            if not missing_identity and not failed_support:
                continue
            original_reason = row["reason"]
            issues[row["id"]] = {
                "code": "unsupported_identity" if missing_identity else "unusable_uncertainty_evidence",
                "model_reason": original_reason,
                "model_identity": copy.deepcopy(identity),
            }
            if missing_identity:
                reason = "The retained review does not establish work identity from usable source content. A URL alone does not verify the cited work."
                row.update(
                    status="unresolved",
                    matched_title=None,
                    reason=reason,
                    corrected_citation=None,
                    identity={"state": "unresolved", "explanation": reason, "supporting_fields": [], "evidence": []},
                    correction_note="No correction proposed; identity remains unverified.",
                )
                row["investigation"] = {
                    "sufficient": False,
                    "scope": "Review of retained source content",
                    "limitations": [original_reason],
                    "searches": [],
                    "candidates_considered": [],
                }
            else:
                row["identity"]["evidence"] = [sid for sid in ids if sid in usable_ids]
            row["evidence"] = [e for e in row["evidence"] if e["source_id"] in usable_ids]
            for check in checks.values():
                check["evidence"] = [sid for sid in check["evidence"] if sid in usable_ids]
            row["model_contract_issue"] = issues[row["id"]]["code"]
        except (KeyError, TypeError, AttributeError):
            continue  # Ordinary shared validation rejects malformed rows.
    return guarded, issues


def check_draft(root, path) -> Any:
    from .assessment import expand, guard_identity
    from .sources import catalog
    from .validation import validate_report

    packet = json.loads((root / "input.json").read_text())
    paths = {root / f"results/{n:03d}.json": r for n, r in enumerate(packet["references"], 1)}
    ref = paths[path]
    try:
        row = json.loads(path.read_text())
        if row.get("id") != ref["id"]:
            raise ValueError("Output ID differs from assigned citation")
        expanded = expand({"schema_version": "compact-3", "results": [row]})
        guarded, _ = guard_identity(expanded, catalog(root))
        report = validate_report(guarded, [ref], root)
        return {"id": ref["id"], "valid": not report["validation_errors"], "errors": report["validation_errors"]}
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return {"id": ref["id"], "valid": False, "errors": {ref["id"]: f"{type(exc).__name__}: {exc}"}}
