"""Distinguish usable source payloads from successful transport responses."""

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlparse

from .arxiv_atom import arxiv_view, is_arxiv_api


def evidence_fingerprint(record) -> Any:
    """Ignore fetch timestamps/attempt logs when checking whether facts changed."""
    payload = {
        key: record[key]
        for key in ("url", "http_status", "body", "body_base64", "text", "metadata", "candidates")
        if key in record
    }
    if isinstance(payload.get("body"), str):
        try:
            payload["body"] = json.loads(payload["body"])
        except ValueError:
            payload["body"] = payload["body"].strip()
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def content_error(record) -> Any:
    """Transport success is not useful evidence when a page contains only its shell."""
    body = record.get("body", "")
    if record.get("media_type") == "application/pdf" or (isinstance(body, str) and body.lstrip().startswith("%PDF-")):
        extracted = record.get("text_extraction", {}).get("status") == "extracted"
        return None if extracted and record.get("text", "").strip() else "PDF response has no extracted readable text"
    if isinstance(body, str) and re.match(
        r"^[\s\ufeff]*(?:<!doctype\s+html|<(?:html|body|head|main|div|script|nav|article)\b)", body, re.IGNORECASE
    ):
        from bs4 import BeautifulSoup

        from .proceedings import html_text

        soup = BeautifulSoup(body, "html.parser")
        # A publisher record may expose bibliographic metadata without visible text.
        if any(tag.get("content", "").strip() for tag in soup.select('meta[name="citation_title"]')):
            return None
        for tag in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(tag.string or tag.get_text())
            except (ValueError, TypeError):
                continue
            items = data if isinstance(data, list) else [data]
            for item in list(items):
                if isinstance(item, dict) and isinstance(item.get("@graph"), list):
                    items.extend(item["@graph"])
            for item in items:
                if not isinstance(item, dict):
                    continue
                kinds = item.get("@type", [])
                kinds = [kinds] if isinstance(kinds, str) else kinds
                if (
                    isinstance(kinds, list)
                    and any(
                        k in {"ScholarlyArticle", "Article", "Book", "Dataset", "SoftwareSourceCode"}
                        for k in kinds
                        if isinstance(k, str)
                    )
                    and (item.get("name") or item.get("headline"))
                ):
                    return None
        if urlparse(record.get("url", "")).hostname == "github.com":
            from .notes import embedded_markdown

            if embedded_markdown(record).strip():
                return None
        visible = html_text(body).strip()
        if not visible or re.fullmatch(
            r"(?:loading[.\s]*|(?:you need to )?(?:please )?enable javascript(?: to (?:run|view|use) (?:this|the) (?:app|page|site))?[.!\s]*)",
            visible,
            re.IGNORECASE,
        ):
            return "HTML page has no readable main content or bibliographic metadata"
        return None
    if any(record.get(key) for key in ("text", "metadata", "candidates")):
        return None
    if not isinstance(body, str) or not body.strip():
        return "Response has no retained content"
    try:
        data = json.loads(body)
    except ValueError:
        return None  # Plain text/XML content is available for evidence review.
    if data in ({}, [], None, ""):
        return "JSON response contains no record or search result information"
    if isinstance(data, dict) and (data.get("error") or data.get("errors")):
        return "JSON response reports an error rather than usable evidence"
    return None


def payload_error(record) -> Any:
    if not 200 <= record.get("http_status", 0) < 300:
        return None  # HTTP failures retain their real status; no synthetic 200.
    host = urlparse(record.get("url", "")).hostname
    body = record.get("body", "")
    if is_arxiv_api(record.get("url", "")):
        return (
            None
            if arxiv_view(record)
            else "Expected a complete, counted arXiv Atom payload; received invalid or error data"
        )
    expected = host in {
        "dblp.org",
        "dblp.uni-trier.de",
        "api.crossref.org",
        "api.datacite.org",
        "api.openalex.org",
        "www.ebi.ac.uk",
    }
    if expected:
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            return "Expected registry JSON; received HTML, a bot challenge, or an invalid response"
        if not isinstance(data, dict):
            return "Expected a registry JSON object"
        if host in {"dblp.org", "dblp.uni-trier.de"} and not isinstance(data.get("result", {}).get("hits"), dict):
            return "DBLP response lacks publication hit metadata"
        if host == "api.openalex.org" and not isinstance(data.get("results"), list) and not data.get("id"):
            return "OpenAlex response lacks work metadata"
        if host == "api.crossref.org" and (data.get("status") == "failed" or "message" not in data):
            return "Crossref response lacks usable metadata"
        if host == "api.datacite.org" and not isinstance(data.get("data"), (dict, list)):
            return "DataCite response lacks usable metadata"
        if host == "www.ebi.ac.uk" and "/europepmc/" in record.get("url", "") and "hitCount" not in data:
            return "Europe PMC response lacks result counts"
    elif isinstance(body, str) and any(
        marker in body.lower() for marker in ("<title>making sure you", 'id="anubis-challenge"', "<title>just a moment")
    ):
        return "Bot challenge is not citation evidence"
    return content_error(record)


def usable(record) -> Any:
    return 200 <= record.get("http_status", 0) < 300 and not record.get("error") and not payload_error(record)
