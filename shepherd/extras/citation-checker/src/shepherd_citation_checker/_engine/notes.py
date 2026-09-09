"""Expose bounded bibliographic notes and their links from retained arXiv HTML."""

import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .response_health import payload_error

FIELDS = {"comments": "comments", "journal reference": "journal_reference", "doi": "doi"}
MAX_TEXT = 2000
MAX_LINKS = 8
MAX_SECTIONS = 6


def embedded_markdown(record) -> Any:
    """Decode retained GitHub blob content; do not scrape navigation or fetch URLs."""
    if (
        urlparse(record.get("url", "")).hostname != "github.com"
        or record.get("http_status") != 200
        or record.get("error")
    ):
        return ""
    body = record.get("body", "")
    if not isinstance(body, str):
        return ""
    soup = BeautifulSoup(body, "html.parser")
    found = []

    def walk(value) -> Any:
        if isinstance(value, dict):
            lines = value.get("rawLines")
            if isinstance(lines, list) and lines and all(isinstance(s, str) for s in lines):
                found.append("\n".join(lines))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for script in soup.find_all("script", type="application/json"):
        try:
            walk(json.loads(script.string or script.get_text()))
        except (ValueError, RecursionError):
            continue
    # A page with conflicting blob payloads is not a single readable document.
    unique = list(dict.fromkeys(found))
    return unique[0] if len(unique) == 1 else ""


def document_notes(record) -> Any:
    host = urlparse(record.get("url", "")).hostname
    sections = []
    if host == "aclanthology.org":
        soup = BeautifulSoup(record.get("body", ""), "html.parser")
        labels = {
            "address": "location",
            "editors": "editors",
            "month": "publication_month",
            "year": "publication_year",
            "pages": "pages",
            "volume": "volume",
        }
        for label in soup.select("dt"):
            field = labels.get(label.get_text(" ", strip=True).rstrip(":").casefold())
            value = label.find_next_sibling("dd")
            if field and value is not None:
                sections.append(
                    {
                        "field": field,
                        "text": value.get_text(" ", strip=True),
                        "links": [],
                        "locator": "dl > dt:" + label.get_text(" ", strip=True),
                    }
                )
    elif host == "github.com":
        markdown = embedded_markdown(record)
        for match in re.finditer(r"```(?:bibtex|bib)\s*\n(.*?)```", markdown, re.DOTALL | re.IGNORECASE):
            sections.append(
                {
                    "field": "suggested_citation",
                    "text": match[1].strip(),
                    "links": [],
                    "locator": "embedded rawLines: citation code block",
                }
            )
        for line in markdown.splitlines():
            if re.match(r"\*\*[^*]*(?:release date|publication date)[^*]*\*\*", line, re.IGNORECASE):
                sections.append(
                    {"field": "release_date", "text": line, "links": [], "locator": "embedded rawLines: labeled date"}
                )
    if not sections:
        return {}
    truncated = len(sections) > 12 or any(len(s["text"]) > MAX_TEXT for s in sections)
    return {
        "sections": [{**s, "text": s["text"][:MAX_TEXT]} for s in sections[:12]],
        "truncated": truncated,
        "provenance": "Explicit labeled fields or citation blocks from this retained document; no inferred publication, identity, or authorship.",
    }


def bibliographic_notes(record) -> Any:
    """Source projection only: no new requests, publication inference or decisions.

    arXiv's HTML puts author-supplied comments and journal references outside its
    citation meta tags. Keep their labels and link destinations attached to the
    same retained source; the linked pages have not necessarily been fetched.
    """
    url = record.get("resolved_url") or record.get("url", "")
    host = urlparse(url).hostname or ""
    body = record.get("body", "")
    if (
        host in {"aclanthology.org", "github.com"}
        and record.get("http_status") == 200
        and isinstance(body, str)
        and not payload_error(record)
    ):
        return document_notes(record)
    if (
        not (host == "arxiv.org" or host.endswith(".arxiv.org"))
        or record.get("http_status") != 200
        or not isinstance(body, str)
        or payload_error(record)
    ):
        return {}
    soup = BeautifulSoup(body, "html.parser")
    sections = []
    truncated = False
    for row in soup.select(".metatable tr"):
        label = row.select_one(".label")
        if label is None:
            continue
        field = FIELDS.get(label.get_text(" ", strip=True).rstrip(":").strip().casefold())
        if field is None:
            continue
        cells = row.find_all("td", recursive=False)
        value = next((c for c in cells if c is not label), None)
        if value is None:
            continue
        text = value.get_text(" ", strip=True)
        links = []
        for anchor in value.select("a[href]"):
            target = urljoin(url, anchor["href"])
            if len(target) > 2048:
                truncated = True
                continue
            parsed = urlparse(target)
            if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
                continue
            link = {"text": anchor.get_text(" ", strip=True)[:160], "url": target}
            if link not in links:
                links.append(link)
        truncated |= len(text) > MAX_TEXT or len(links) > MAX_LINKS
        sections.append({"field": field, "text": text[:MAX_TEXT], "links": links[:MAX_LINKS]})
    if not sections:
        return {}
    return {
        "sections": sections[:MAX_SECTIONS],
        "truncated": truncated or len(sections) > MAX_SECTIONS,
        "provenance": "Text and links from this retained arXiv page's bibliographic table; comments may be author-supplied. Link association is source evidence, not proof that a linked page was fetched or is currently accessible.",
    }
