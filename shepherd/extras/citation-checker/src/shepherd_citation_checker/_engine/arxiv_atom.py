"""Project counted arXiv Atom responses, including real zero-hit searches."""

import xml.etree.ElementTree as ET
from functools import lru_cache
from typing import Any
from urllib.parse import parse_qs, urlparse

ATOM = "{http://www.w3.org/2005/Atom}"
SEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"
ARXIV = "{http://arxiv.org/schemas/atom}"


def is_arxiv_api(url) -> Any:
    parsed = urlparse(url)
    return parsed.hostname in {"export.arxiv.org", "arxiv.org", "www.arxiv.org"} and parsed.path == "/api/query"


def arxiv_view(record) -> Any:
    if (
        not is_arxiv_api(record.get("url", ""))
        or record.get("http_status") != 200
        or record.get("error")
        or record.get("truncated")
    ):
        return None
    body = record.get("body", "")
    if not isinstance(body, str):
        return None
    return _parse(record["url"], body)


@lru_cache(maxsize=32)
def _parse(url, body) -> Any:
    # Disallow entity/DTD expansion; API Atom has neither declaration.
    if "<!DOCTYPE" in body.upper() or "<!ENTITY" in body.upper():
        return None
    try:
        feed = ET.fromstring(body)  # noqa: S314 -- DTD/entity declarations rejected above
        if feed.tag != ATOM + "feed":
            return None
        total = int(feed.findtext(SEARCH + "totalResults", ""))
        start = int(feed.findtext(SEARCH + "startIndex", ""))
        size = int(feed.findtext(SEARCH + "itemsPerPage", ""))
        query = parse_qs(urlparse(url).query)
        if min(total, start, size) < 0 or start != int(query.get("start", [0])[0]) or start > total:
            return None
        items = []
        for entry in feed.findall(ATOM + "entry"):
            identifier = entry.findtext(ATOM + "id", "").strip()
            target = urlparse(identifier)
            title = " ".join(entry.findtext(ATOM + "title", "").split())
            authors = [a.findtext(ATOM + "name", "").strip() for a in entry.findall(ATOM + "author")]
            if (
                target.hostname not in {"arxiv.org", "www.arxiv.org"}
                or not target.path.startswith("/abs/")
                or not target.path.removeprefix("/abs/")
                or not title
                or title.casefold() == "error"
                or not authors
                or not all(authors)
            ):
                return None
            items.append(
                {
                    "candidate_id": target.path.removeprefix("/abs/"),
                    "title": title,
                    "authors": authors,
                    "URL": identifier,
                    "published": entry.findtext(ATOM + "published"),
                    "doi": entry.findtext(ARXIV + "doi"),
                    "journal_reference": entry.findtext(ARXIV + "journal_ref"),
                }
            )
        if len(items) > size or start + len(items) > total:
            return None
        return {"total": total, "start": start, "size": size, "candidates": items}
    except (ET.ParseError, ValueError, TypeError):
        return None
