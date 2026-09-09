"""Enforce adjudication tool budgets and retain native search results automatically."""

import fcntl
import hashlib
import json
import shlex
import sys
from pathlib import Path

# ruff: noqa: T201 -- tool and CLI response protocol
from typing import Any


def consume(root, key, limit) -> Any:
    path = root / "tool-budget.json"
    with path.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        content = stream.read()
        counts = json.loads(content) if content else {}
        count = counts.get(key, 0)
        if count >= limit:
            raise ValueError(f"{key} budget exhausted ({limit})")
        counts[key] = count + 1
        stream.seek(0)
        stream.truncate()
        json.dump(counts, stream)


def authorize(root, event) -> Any:
    tool = event["tool_name"]
    args = event.get("tool_input", {})
    if tool == "WebSearch":
        limits = json.loads((root / "adjudication-limits.json").read_text())
        consume(root, "searches", limits["searches"])
    elif tool == "Write":
        inputs = root / "input.json"
        data = json.loads(inputs.read_text()) if inputs.exists() else {}
        target = Path(args.get("file_path", "")).resolve()
        allowed = {(root / f"results/{n:03d}.json").resolve() for n, _ in enumerate(data["references"], 1)}
        if target in allowed:
            from .assessment import check_draft

            if target.exists() and check_draft(root, target)["valid"]:
                raise ValueError("Validated citation output is final")
            consume(root, "writes:" + target.name, 3)
        elif target not in allowed or target.exists():
            raise ValueError("Write each assigned citation output once; unknown paths and overwrites are forbidden")
    elif tool == "Bash":
        parts = shlex.split(args.get("command", ""))
        valid = parts == ["python3", "-B", "-m", "shepherd_citation_checker._engine.validation"] or (
            len(parts) == 6
            and parts[:5] == ["python3", "-B", "-m", "shepherd_citation_checker._engine.documents", "fetch"]
            and parts[5].startswith(("https://", "http://"))
        )
        if len(parts) == 6 and parts[:5] == [
            "python3",
            "-B",
            "-m",
            "shepherd_citation_checker._engine.documents",
            "fetch-many",
        ]:
            data = json.loads((root / "input.json").read_text())
            urls = json.loads(parts[5])
            valid = (
                isinstance(urls, list)
                and (1 <= len(urls) <= 6)
                and all(isinstance(u, str) and u.startswith(("https://", "http://")) for u in urls)
            )
        spellings = {shlex.join(parts)}
        if valid and len(parts) == 6:
            prefix, url = (shlex.join(parts[:5]), parts[5])
            if "'" not in url:
                spellings.add(f"{prefix} '{url}'")
            if not any(c in url for c in ("$", "`", '"', "\\")):
                spellings.add(f'{prefix} "{url}"')
        if not valid or args.get("command") not in spellings or args.get("run_in_background"):
            raise ValueError("Use Read/Grep/Glob for inspection; Bash is limited to the retained fetcher and validator")
    elif tool not in {"Read", "Grep", "Glob"}:
        raise ValueError("Tool not enabled for citation adjudication")


def handle(root, event) -> Any:
    if event.get("hook_event_name") == "PreToolUse":
        authorize(root, event)
    elif event.get("hook_event_name") == "PostToolUse" and event.get("tool_name") == "WebSearch":
        directory = root / "search-results"
        directory.mkdir(exist_ok=True)
        data = {k: event.get(k) for k in ("tool_use_id", "tool_name", "tool_input", "tool_response")}
        encoded = json.dumps(data, sort_keys=True)
        (directory / (hashlib.sha256(encoded.encode()).hexdigest()[:20] + ".json")).write_text(encoded + "\n")
    elif event.get("hook_event_name") == "PostToolUse" and event.get("tool_name") == "Write":
        from .assessment import check_draft

        result = check_draft(root, Path(event["tool_input"]["file_path"]).resolve())
        if not result["valid"]:
            raise ValueError("Draft invalid; repair this output: " + json.dumps(result))
        print(
            json.dumps(
                {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": json.dumps(result)}}
            )
        )


if __name__ == "__main__":
    try:
        handle(Path.cwd(), json.load(sys.stdin))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
