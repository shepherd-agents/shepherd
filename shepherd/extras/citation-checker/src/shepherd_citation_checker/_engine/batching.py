"""Deterministic workload estimates for bounded citation batches, without verdicts."""

import json
import math
from typing import Any

from .sources import compact, preview_packet, required_fields


def estimate(ref, sources) -> Any:
    packet = preview_packet(
        {
            "references": [{"id": ref["id"], "raw": ref["raw"]}],
            "sources": [compact(s["record"]) for s in sources.values()],
        }
    )
    fields = max(5, len(required_fields(ref["raw"])))
    return {
        "id": ref["id"],
        "input_tokens": math.ceil(len(json.dumps(packet, ensure_ascii=False)) / 4),
        "output_tokens": 200 + fields * 45 + min(400, len(ref["raw"]) // 8),
    }


def pack(items, *, max_citations=6, max_input_tokens=12000, max_output_tokens=3200) -> Any:
    if min(max_citations, max_input_tokens, max_output_tokens) <= 0:
        raise ValueError("Batch limits must be positive")
    count = max(
        math.ceil(len(items) / max_citations),
        math.ceil(sum(i["input_tokens"] for i in items) / max_input_tokens),
        math.ceil(sum(i["output_tokens"] for i in items) / max_output_tokens),
    )
    bins = [{"ids": [], "input_tokens": 0, "output_tokens": 0} for _ in range(count)]
    weights = {i["id"]: i["input_tokens"] / max_input_tokens + i["output_tokens"] / max_output_tokens for i in items}
    for item in sorted(items, key=lambda i: (-weights[i["id"]], i["id"])):
        fits = [
            b
            for b in bins
            if len(b["ids"]) < max_citations
            and b["input_tokens"] + item["input_tokens"] <= max_input_tokens
            and b["output_tokens"] + item["output_tokens"] <= max_output_tokens
        ]
        if not fits:
            batch = {"ids": [], "input_tokens": 0, "output_tokens": 0}
            bins.append(batch)
        else:
            batch = min(
                fits, key=lambda b: b["input_tokens"] / max_input_tokens + b["output_tokens"] / max_output_tokens
            )
        batch["ids"].append(item["id"])
        for field in ("input_tokens", "output_tokens"):
            batch[field] += item[field]
    result = [b for b in bins if b["ids"]]
    for batch in result:
        batch["ids"].sort(key=lambda i: (weights[i], i))
        batch["oversized_singleton"] = (
            batch["input_tokens"] > max_input_tokens or batch["output_tokens"] > max_output_tokens
        )
    return result
