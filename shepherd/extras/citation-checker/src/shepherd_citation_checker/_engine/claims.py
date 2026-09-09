"""Ground warnings in supplied claims; preserve substantive bibliographic differences."""

import html
import json
import re
import unicodedata
from contextlib import suppress
from typing import Any


def surface(value) -> Any:
    value = unicodedata.normalize("NFC", html.unescape(value))
    value = value.translate(str.maketrans({"\u2013": "-", "\u2014": "-", "\u2019": "'", "\u00a0": " "}))
    return " ".join(value.split())


def comparison_key(field, value) -> Any:
    value = surface(value).casefold()
    if field in {"identifier", "doi", "url"}:
        value = re.sub(r"\s+", "", value)
        value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)", "", value)
    if field in {"page", "pages"}:
        value = re.sub(r"^pp?\.\s*", "", value)
        value = re.sub(r"\s*-\s*", "-", value)
    return value


def bare_url(raw) -> Any:
    value = surface(raw)
    value = re.sub(r"(?<=\.)\s+(?=(?:com|org|net)\b)", "", value)
    return bool(re.fullmatch(r"(?:https?://\S+\s*\.?\s*(?:URL\s+)?)+", value))


def required_claims(raw) -> Any:
    from .sources import required_fields

    if bare_url(raw):
        return ["url"]
    fields = set(required_fields(raw))
    if re.search(r"arxiv\.\s*org/(?:abs|pdf)/\s*\d{4}\.\s*\d{4,5}", raw, re.IGNORECASE):
        fields.add("identifier")
    if any("preprint" not in value.casefold() for value in work_type_annotations(raw)):
        fields.add("work_type")
    if re.search(r"\b(?:\d+(?:st|nd|rd|th)|first|second|third|revised)\s+edition\b", raw, re.IGNORECASE):
        fields.add("edition")
    return sorted(fields)


def work_type_annotations(raw) -> Any:
    return re.findall(
        r"(?:^|[.;\n\[(])\s*(white\s+paper|blog\s+post|technical\s+report|(?:arxiv\s+)?preprint)"
        r"(?=\s*(?:[.;\])]|$)|\s+arxiv:)",
        surface(raw),
        re.IGNORECASE,
    )


def explicit_annotation(raw, field) -> Any:
    """Recognize explicit annotations, not words embedded in a work's title."""
    if field == "work_type":
        return bool(work_type_annotations(raw))
    if field == "version":
        return bool(re.search(r"\b(?:version\s+|v\.?\s*)\d+(?:\.\d+)*\b", raw, re.IGNORECASE))
    if field == "edition":
        return bool(re.search(r"\b(?:\d+(?:st|nd|rd|th)|first|second|third|revised)\s+edition\b", raw, re.IGNORECASE))
    return True


def source_text(record) -> Any:
    def strings(value) -> Any:
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for child in value.values():
                yield from strings(child)
        elif isinstance(value, list):
            for child in value:
                yield from strings(child)

    body = record.get("body", "")
    raw_body = body
    with suppress(ValueError):
        body = json.loads(body) if isinstance(body, str) else body
    values = list(strings(body)) + list(strings(record.get("text", "")))
    if isinstance(raw_body, str):
        # Numeric JSON metadata (e.g. date-parts [[2023,6]]) is also retained
        # evidence. Keep its literal representation alongside decoded strings.
        values.append(raw_body)
    if isinstance(body, (dict, list)):
        values.append(json.dumps(body, ensure_ascii=False, separators=(",", ":")))
    # Keep raw strings as well as readable HTML; do not invent joined quotations.
    return [surface(v) for s in values for v in (s, re.sub(r"<[^>]+>", " ", s))]


def json_excerpt(value) -> Any:
    """Normalize JSON formatting without changing string contents or values."""
    try:
        parsed, wrapped = json.loads(value), False
    except ValueError:
        try:
            parsed, wrapped = json.loads("{" + value + "}"), True
        except ValueError:
            return None
    if not isinstance(parsed, (dict, list)):
        return None
    encoded = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    return encoded[1:-1] if wrapped else encoded


def validate_claims(result, ref, sources) -> Any:
    checks = result["field_checks"]
    claims = result["claim_spans"]
    absent = result["not_supplied"]
    if not isinstance(claims, dict) or not isinstance(absent, list):
        raise TypeError("Expected claim spans and not_supplied fields")
    if set(absent) & set(checks):
        raise ValueError("Absent fields cannot carry verification findings")
    raw = surface(ref["raw"])
    for field, claim in claims.items():
        present = isinstance(claim, str) and bool(claim.strip()) and surface(claim) in raw
        if not present and field in {"url", "identifier", "doi"} and isinstance(claim, str) and claim.strip():
            # PDF line wrapping inside identifiers/URLs does not change their value.
            # Restrict this to identifier fields; do not concatenate ordinary words.
            present = re.sub(r"\s+", "", surface(claim)) in re.sub(r"\s+", "", raw)
        if field not in checks or not present:
            raise ValueError(f"Claim span must occur in the citation: {field}")
    for field, check in checks.items():
        if field in {"work_type", "version", "edition"} and not explicit_annotation(ref["raw"], field):
            raise ValueError(f"No explicit supplied {field} annotation; omit its check and use enrichment only")
        if (
            field == "work_type"
            and field in claims
            and comparison_key(field, claims[field])
            not in {comparison_key(field, value) for value in work_type_annotations(ref["raw"])}
        ):
            raise ValueError("Work-type claim must quote the separate supplied annotation")
        if field in {"version", "edition"} and field in claims and not explicit_annotation(claims[field], field):
            raise ValueError(f"Claim span must include the explicit {field} value")
        if check["outcome"] in {"uncertain", "error"} and field not in claims:
            raise ValueError(f"Warning needs a supplied citation span: {field}")
        if check["outcome"] != "error":
            continue
        contradiction = result["contradictions"][field]
        sid, quote = contradiction["source_id"], contradiction["quote"]
        quotes = [quote] if isinstance(quote, str) else quote
        if (
            sid not in check["evidence"]
            or not isinstance(quotes, list)
            or not quotes
            or any(not isinstance(q, str) or not q.strip() for q in quotes)
        ):
            raise ValueError("Error needs a quoted decision source")
        retained = source_text(sources[sid]["record"])
        quoted_forms = [(surface(q), json_excerpt(q)) for q in quotes]
        if not all(
            any(literal in text or (normalized is not None and normalized in text) for text in retained)
            for literal, normalized in quoted_forms
        ):
            raise ValueError(f"Contradictory excerpt not found in retained source: {field}")
        if not isinstance(contradiction["same_work"], str) or not contradiction["same_work"].strip():
            raise ValueError("Error needs work/version applicability explanation")
        if len(quotes) == 1 and comparison_key(field, claims[field]) == comparison_key(field, quotes[0]):
            raise ValueError("Equivalent spelling is not a contradiction")
