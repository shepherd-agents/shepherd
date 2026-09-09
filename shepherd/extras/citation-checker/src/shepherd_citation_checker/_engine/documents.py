"""Store complete documents and a source index; no relevance ranking or model calls."""

import base64
import concurrent.futures
import json
import sys
from collections import defaultdict
from pathlib import Path

# ruff: noqa: T201 -- tool and CLI response protocol
from typing import Any
from urllib.parse import urlparse

from .notes import bibliographic_notes, embedded_markdown
from .proceedings import html_text
from .response_health import payload_error
from .sources import catalog, source_id


def materialize(root) -> Any:
    documents = root / "documents"
    documents.mkdir(exist_ok=True)
    index = []
    for sid, source in catalog(root).items():
        record = source["record"]
        body = record.get("body", "")
        paths = {}
        if record.get("body_base64"):
            path = documents / (sid + ".pdf")
            path.write_bytes(base64.b64decode(record["body_base64"], validate=True))
            paths["original"] = path.relative_to(root).as_posix()
            text = record.get("text", "")
        else:
            path = documents / (sid + ".source")
            path.write_text(body if isinstance(body, str) else json.dumps(body, indent=2))
            paths["original"] = path.relative_to(root).as_posix()
            if isinstance(body, str) and "<" in body and (not body.lstrip().startswith(("{", "[", "%PDF-"))):
                text = embedded_markdown(record) or html_text(body)
            else:
                text = record.get("text", "")
                if isinstance(body, str) and body.lstrip().startswith("%PDF-"):
                    text = ""
        if text:
            path = documents / (sid + ".txt")
            path.write_text(text)
            paths["text"] = path.relative_to(root).as_posix()
        index.append(
            {
                "source_id": sid,
                "url": record["url"],
                "http_status": record.get("http_status"),
                "evidence_file": source["evidence_file"],
                **paths,
                "metadata": record.get("metadata", {}),
                "bibliographic_notes": bibliographic_notes(record),
                "content_error": payload_error(record),
                "retrieval_error": record.get("error"),
                "truncated": record.get("truncated", False),
                "text_extraction": record.get("text_extraction"),
            }
        )
    (root / "source-index.json").write_text(json.dumps(index, indent=2) + "\n")
    return index


def fetch_many(root, urls) -> Any:
    from .fetch import fetch
    from .hooks import consume
    from .network import public_url

    limits = json.loads((root / "adjudication-limits.json").read_text())
    if not isinstance(urls, list) or not 1 <= len(urls) <= 6 or (not all(isinstance(u, str) for u in urls)):
        raise ValueError("Expected one to six URL strings")
    groups = defaultdict(list)
    for position, url in enumerate(urls):
        public_url(url)
        groups[urlparse(url).hostname].append((position, url))
    blocked = {
        urlparse(s["record"]["url"]).hostname
        for s in catalog(root).values()
        if s["evidence_file"].startswith("evidence/extra/") and s["record"].get("http_status") == 429
    }

    def fetch_host(host, items) -> Any:
        stopped = host in blocked
        outcomes = []
        for position, url in items:
            if stopped:
                outcomes.append(
                    (
                        position,
                        None,
                        {
                            "url": url,
                            "retrieval_error": "Skipped: host returned HTTP 429 in this batch",
                            "skipped": True,
                        },
                    )
                )
                continue
            try:
                consume(root, "fetches", limits["fetches"])
                record = fetch(url, root / "evidence" / "extra", validate_url=public_url)
                stopped = record.get("http_status") == 429
                outcomes.append((position, record, None))
            except Exception as exc:  # noqa: BLE001 -- preserve failure receipts at the process/tool boundary
                outcomes.append((position, None, {"url": url, "retrieval_error": f"{type(exc).__name__}: {exc}"}))
        return outcomes

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(groups))) as pool:
        futures = [pool.submit(fetch_host, host, items) for host, items in groups.items()]
        outcomes = sorted(item for future in futures for item in future.result())
    materialize(root)
    index = {r["source_id"]: r for r in json.loads((root / "source-index.json").read_text())}
    from .sources import candidates, preview_packet, search_extent

    result = []
    for _, record, failure in outcomes:
        if failure is not None:
            result.append(failure)
            continue
        row = index[source_id(record)]
        items = candidates(record)
        row.update(candidates=items[:10], search_extent=search_extent(record))
        row = preview_packet({"sources": [row]})["sources"][0]
        while row["candidates"] and len(json.dumps(row)) > 30000:
            row["candidates"].pop()
        row["candidate_preview"] = {
            "shown": len(row["candidates"]),
            "retained": len(items),
            "complete": len(row["candidates"]) == len(items),
        }
        result.append(row)
    return result


def retrieve(root, url) -> Any:
    """Fetch one URL through the same bounded collector."""
    return fetch_many(root, [url])[0]


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in {"fetch", "fetch-many"}:
        raise SystemExit(
            "Usage: python3 -B -m shepherd_citation_checker._engine.documents fetch URL | fetch-many JSON_URL_LIST"
        )
    value = (
        fetch_many(Path.cwd(), json.loads(sys.argv[2]))
        if sys.argv[1] == "fetch-many"
        else retrieve(Path.cwd(), sys.argv[2])
    )
    print(json.dumps(value, indent=2))
