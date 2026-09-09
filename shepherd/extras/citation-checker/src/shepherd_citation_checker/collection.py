"""Initial evidence collection; no benchmark or reviewer-result dependencies."""

import re
import time
from typing import Any
from urllib.parse import quote, urlencode

from ._engine.fetch import fetch
from ._engine.network import public_url
from ._engine.sources import candidates
from .storage import digest, read, write


def initial_urls(raw) -> Any:
    """Plan identifier and bibliographic lookups from supplied text."""
    flat = re.sub(r"\s+", " ", raw)
    flat = re.sub(r"(\d{4}\.)\s+(\d{4,5})", r"\1\2", flat)
    urls = [f"https://arxiv.org/abs/{v}" for v in dict.fromkeys(re.findall(r"\b\d{4}\.\d{4,5}(?:v\d+)?\b", flat))]
    dois = list(dict.fromkeys(d.rstrip(".,;") for d in re.findall(r"10\.\d{4,9}/[^\s]+", re.sub(r"/\s+", "/", flat))))
    for doi in dois:
        urls.extend(
            ["https://api.crossref.org/works/" + quote(doi, safe=""), "https://doi.org/" + quote(doi, safe="/")]
        )
    if not urls or dois:
        url_text = re.sub(r"\n(?=[A-Za-z][A-Za-z_-]*\s*:)", " ", raw).replace("\n", "")
        urls.extend(u.rstrip(".,;") for u in re.findall(r"https?://[^\s]+", url_text))
        query = re.sub(r"https?://\S+|10\.\d{4,9}/\S+", "", flat).strip()
        urls.append("https://api.crossref.org/works?" + urlencode({"query.bibliographic": query, "rows": 3}))
    return list(dict.fromkeys(urls))


def collect(citations_path) -> Any:
    """Collect or reuse initial source records without consulting verdicts."""
    root = citations_path.parent
    request = read(root / "request.json")
    refs = read(citations_path)["references"]
    cache = root / "evidence"
    cache.mkdir(exist_ok=True)
    fetched = {}
    for ref in refs:
        paths = []
        for seed in request["evidence"].get(ref["id"], []):
            source = root / seed["path"]
            if digest(source) != seed["sha256"]:
                raise ValueError("Supplied source changed")
            paths.append({"evidence_file": seed["path"]})
        if request["retrieve_initial"]:
            for url in initial_urls(ref["raw"]):
                if url not in fetched:
                    fetched[url] = fetch(url, cache, validate_url=public_url)
                    time.sleep(1.05 if "api.crossref.org" in url else 0.25)
                records = [fetched[url]]
                if url.startswith("https://api.crossref.org/works/") and not candidates(records[0]):
                    fallback = "https://api.datacite.org/dois/" + url.rsplit("/", 1)[1]
                    if fallback not in fetched:
                        fetched[fallback] = fetch(fallback, cache, validate_url=public_url)
                    records.append(fetched[fallback])
                for record in records:
                    # The fetcher already retains its bytes; give the citation a
                    # canonical explicit path independent of response formatting.
                    import hashlib
                    import json

                    key = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
                    target = cache / (key + ".json")
                    write(target, record)
                    paths.append({"evidence_file": target.relative_to(root).as_posix()})
        ref["prefetched"] = list({p["evidence_file"]: p for p in paths}.values())
    write(root / "evidence-input.json", {"references": refs})
    write(root / "evidence-manifest.json", {p.relative_to(root).as_posix(): digest(p) for p in cache.rglob("*.json")})
    return str(root / "evidence-input.json")
