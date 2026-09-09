"""Read full retained official proceedings; never mistake a preview for the index."""

import json
import re
import sys
from functools import lru_cache
from pathlib import Path

# ruff: noqa: T201 -- tool and CLI response protocol
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


def proceedings_view(record) -> Any:
    if record.get("http_status") != 200 or record.get("error") or record.get("truncated"):
        return None
    url = urlparse(record.get("url", ""))
    if url.query or url.fragment:
        return None
    supported = (url.hostname == "proceedings.mlr.press" and re.fullmatch(r"/v\d+/", url.path)) or (
        url.hostname == "aclanthology.org" and re.fullmatch(r"/volumes/[\w.-]+/", url.path)
    )
    if not supported:
        return None
    return _parse(record["url"], record.get("body", ""))


@lru_cache(maxsize=8)
def _parse(url, body) -> Any:
    if not isinstance(body, str) or "</html>" not in body.lower() or "</body>" not in body.lower():
        return None
    parsed = urlparse(url)
    soup = BeautifulSoup(body, "html.parser")
    if soup.select('a[rel="next"], .pagination'):
        return None
    items = []
    if parsed.hostname == "proceedings.mlr.press":
        volume = parsed.path.strip("/v")
        heading = soup.find(["h1", "h2"])
        if not heading or not re.search(rf"\bVolume\s+{volume}\b", heading.get_text(" ", strip=True)):
            return None
        blocks = soup.select(".paper")
        for block in blocks:
            title, authors = block.select_one(".title"), block.select_one(".authors")
            link = next((a for a in block.select("a[href]") if a.get_text(strip=True) == "abs"), None)
            if not title or not authors or not link:
                return None
            target = urljoin(url, link["href"])
            if urlparse(target).hostname != parsed.hostname or not urlparse(target).path.startswith(parsed.path):
                return None
            items.append(
                {
                    "candidate_id": target,
                    "title": title.get_text(" ", strip=True),
                    "authors": authors.get_text(" ", strip=True),
                    "URL": target,
                }
            )
    else:
        volume = parsed.path.strip("/").split("/")[-1]
        if not soup.title or "ACL Anthology" not in soup.title.get_text():
            return None
        for link in soup.select("strong a[href]"):
            target = urlparse(urljoin(url, link["href"]))
            match = re.fullmatch(rf"/{re.escape(volume)}\.(\d+)/", target.path)
            if target.hostname != parsed.hostname or not match or int(match[1]) == 0:
                continue
            block = link.find_parent("span") or link.parent.parent
            authors = [a.get_text(" ", strip=True) for a in block.select('a[href^="/people/"]')]
            if not authors:
                return None
            items.append(
                {
                    "candidate_id": target.path.strip("/"),
                    "title": link.get_text(" ", strip=True),
                    "authors": authors,
                    "URL": urljoin(url, link["href"]),
                }
            )
    if not items or any(not item["title"] or not item["authors"] for item in items):
        return None
    if len({item["candidate_id"] for item in items}) != len(items):
        return None
    return {"kind": "official_proceedings", "scope": url, "complete": True, "candidates": items}


def html_text(body) -> Any:
    """Remove navigation and scripts before producing a bounded text preview."""
    soup = BeautifulSoup(body, "html.parser")
    for node in soup.select("script, style, nav, header, footer, noscript"):
        node.decompose()
    content = soup.find("main") or soup.find("article") or soup.body or soup
    return content.get_text(" ", strip=True)


def candidate_subset(record, query, citation_text) -> Any:
    """Apply an explicit OR of citation title terms to a complete volume index."""
    view = proceedings_view(record)
    if not view or not isinstance(query, dict) or set(query) != {"title_any"}:
        raise ValueError("Scoped candidate queries require a complete official proceedings index and title_any")
    terms = query["title_any"]

    def normalize(value) -> Any:
        return " ".join(re.findall(r"\w+", value.casefold()))

    if not isinstance(terms, list) or not 1 <= len(terms) <= 8:
        raise ValueError("Provide one to eight citation title terms")
    text = normalize(citation_text)
    if any(not isinstance(t, str) or len(normalize(t)) < 4 or normalize(t) not in text for t in terms):
        raise ValueError("Query terms must occur in the supplied citation; invented terms cannot close coverage")
    return [item for item in view["candidates"] if any(normalize(term) in normalize(item["title"]) for term in terms)]


if __name__ == "__main__":
    from .sources import catalog

    source, *terms = sys.argv[1:]
    root = Path.cwd()
    record = catalog(root)[source]["record"]
    query = {"title_any": terms}
    text = "\n".join(r["raw"] for r in json.loads((root / "input.json").read_text())["references"])
    found = candidate_subset(record, query, text)
    print(
        json.dumps(
            {
                "source_id": source,
                "source_query": query,
                "scope": record["url"],
                "query_hits": len(found),
                "candidates": found,
                "note": "Complete within this explicit title-term query over the retained volume, not across all alternative titles.",
            }
        )
    )
