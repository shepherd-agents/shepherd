"""User-facing metadata assessment; native benchmark scoring stays independent."""

from typing import Any

OPTIONAL_DETAILS = frozenset(
    {
        "page",
        "pages",
        "page_range",
        "start_page",
        "end_page",
        "first_page",
        "last_page",
        "volume",
        "issue",
        "editor",
        "editors",
        "location",
        "publisher_location",
        "conference_location",
        "place",
        "issn",
        "edition",
        "version",
        "work_type",
        "article_number",
    }
)


def optional_detail(field) -> Any:
    return field.strip().lower().replace("-", "_").replace(" ", "_") in OPTIONAL_DETAILS


def assessment(row) -> Any:
    checks = row.get("field_checks", {})
    errors = sorted(k for k, v in checks.items() if v["outcome"] == "error")
    optional = sorted(k for k, v in checks.items() if v["outcome"] == "uncertain" and optional_detail(k))
    core = sorted(k for k, v in checks.items() if v["outcome"] == "uncertain" and not optional_detail(k))
    state = row["identity"]["state"]
    if state != "identified":
        headline = {
            "ambiguous": "Multiple plausible works; identity needs review",
            "unresolved": "Insufficient evidence to identify the work",
            "not_found": "No matching work found in the searched sources",
        }[state]
    elif errors:
        headline = "Work identified; citation corrections needed"
    elif core:
        headline = "Work identified; key citation details remain unverified"
    elif optional:
        headline = "Work identified; optional bibliographic details remain unverified"
    else:
        headline = "Work identified; no confirmed citation errors"
    return {
        "attention": "issue"
        if errors or state == "not_found"
        else "verification_gap"
        if state != "identified" or core
        else "none",
        "headline": headline,
        "confirmed_error_fields": errors,
        "unverified_core_fields": core,
        "unverified_optional_fields": optional,
        "work_identity_verified": state == "identified",
        "metadata_complete": state == "identified" and bool(checks) and not errors and not core and not optional,
    }
