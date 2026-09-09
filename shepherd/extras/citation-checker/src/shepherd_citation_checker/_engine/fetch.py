"""Bounded public HTTP retrieval for the model's current missing-evidence task."""

import base64
import hashlib
import io
import json
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

# ruff: noqa: T201 -- tool and CLI response protocol
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .proceedings import html_text
from .sources import catalog, compact


class Metadata(HTMLParser):
    def __init__(self) -> Any:
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs) -> Any:
        values = dict(attrs)
        if tag == "meta" and values.get("name", "").startswith("citation_"):
            self.values.setdefault(values["name"], []).append(values.get("content", ""))


def fetch(url, directory, validate_url=None) -> Any:
    if urlparse(url).scheme not in {"http", "https"}:
        raise ValueError("Only HTTP(S) retrieval is permitted")
    if validate_url:
        validate_url(url)

    class Redirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl) -> Any:
            if validate_url:
                validate_url(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = build_opener(Redirect())
    record = {"url": url, "retrieved_at": datetime.now(timezone.utc).isoformat(), "attempts": []}
    headers = {"User-Agent": "CitationCheckerDebug/3.0"}
    if urlparse(url).hostname == "doi.org":
        headers["Accept"] = "application/vnd.citationstyles.csl+json"
    deadline = time.monotonic() + 45
    for attempt in range(2):
        try:
            try:
                response = opener.open(Request(url, headers=headers), timeout=20)  # noqa: S310 -- scheme checked above
            except HTTPError as exc:
                response = exc
            with response:
                parts, size = [], 0
                while size <= 4_000_000 and time.monotonic() < deadline:
                    part = response.read1(min(65536, 4_000_001 - size))
                    if not part:
                        break
                    parts.append(part)
                    size += len(part)
                body = b"".join(parts)
                record.update(
                    http_status=response.code,
                    resolved_url=response.url,
                    retry_after=response.headers.get("Retry-After"),
                    body=body[:4_000_000].decode("utf-8", errors="replace"),
                    truncated=len(body) > 4_000_000 or time.monotonic() >= deadline,
                )
                if body.startswith(b"%PDF-"):
                    record["media_type"] = "application/pdf"
                    record["body_base64"] = base64.b64encode(body[:4_000_000]).decode("ascii")
                    record["body"] = ""
            record["attempts"].append({"http_status": record["http_status"]})
            if record["http_status"] == 429:
                break  # Honor server cooldown in host scheduler, never retry quota errors here.
            if record["http_status"] not in {500, 502, 503, 504}:
                break
        except (URLError, TimeoutError, OSError) as exc:
            record["attempts"].append({"error": str(exc)})
            record["error"] = str(exc)
        if time.monotonic() >= deadline:
            break
        if attempt < 1:
            time.sleep(2**attempt)
    if 200 <= record.get("http_status", 0) < 300:
        record.pop("error", None)
        if record.get("media_type") == "application/pdf":
            record["text"] = ""
            record["text_extraction"] = {"method": "pdfplumber", "status": "unavailable"}
            try:
                import pdfplumber

                with pdfplumber.open(io.BytesIO(base64.b64decode(record["body_base64"]))) as pdf:
                    texts = [page.extract_text() or "" for page in pdf.pages[:40]]
                    record["text"] = "\n\n".join(f"[Page {i}]\n{t}" for i, t in enumerate(texts, 1) if t.strip())
                    record["text_extraction"] = {
                        "method": "pdfplumber",
                        "status": "extracted" if record["text"].strip() else "no_text",
                        "page_count": len(pdf.pages),
                        "pages_examined": len(texts),
                        "truncated": len(pdf.pages) > 40,
                    }
            except Exception as exc:  # noqa: BLE001 -- preserve failure receipts at the process/tool boundary
                record["text_extraction"]["error"] = f"{type(exc).__name__}: {exc}"
        try:
            data = json.loads(record["body"])
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("title"):
            record["candidates"] = [data]
        elif data is None and record.get("media_type") != "application/pdf":
            parser = Metadata()
            parser.feed(record.get("body", ""))
            record["metadata"] = parser.values
            record["text"] = html_text(record.get("body", ""))[:12000]
    directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()[:20]
    temporary = directory / f"{key}.tmp"
    temporary.write_text(json.dumps(record, indent=2) + "\n")
    temporary.replace(directory / f"{key}.json")
    return record


def main() -> Any:
    """Run the installed citation-checker command."""
    root = Path.cwd()
    if not json.loads((root / "input.json").read_text())["search_allowed"]:
        raise ValueError("Network retrieval is disabled for this evidence-only attempt")
    url = sys.argv[1]
    if urlparse(url).scheme not in {"http", "https"}:
        raise ValueError("Only actual HTTP(S) source URLs are accepted")
    existing = [v["record"] for v in catalog(root).values() if v["record"]["url"] == url]
    successful = [r for r in existing if 200 <= r.get("http_status", 0) < 300]
    record = successful[-1] if successful else fetch(url, root / "evidence/extra")
    print(json.dumps(compact(record)))


if __name__ == "__main__":
    main()
