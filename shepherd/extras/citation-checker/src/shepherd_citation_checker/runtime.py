"""Isolated Shepherd headless transport, deadlines and durable checkpoints."""

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from ._engine.checkpoints import ObservedExecution, finalize, recover_hard_stop
from .config import CheckerConfig
from .storage import digest, read, write


def auth_check() -> Any:
    """Check subscription authentication and native-jail availability."""
    from shepherd_dialect import claude_auth_status, native_jail_available

    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        os.environ.pop(key, None)
    auth = claude_auth_status()
    if not auth.ok or auth.mode != "subscription_login":
        raise RuntimeError("Subscription authentication unavailable: run claude auth login")
    if not native_jail_available():
        raise RuntimeError("Native jail unavailable")


@contextmanager
def provider_adapter(job, request, protected, rendered) -> Any:
    # Workspace runtime currently exposes provider/model only. Confine overrides
    # to this single-purpose worker process and restore them even on failure.
    """Apply invocation-local provider options and restore them on exit."""
    from shepherd_dialect.providers import ClaudeHeadlessProvider

    execute, argv = ClaudeHeadlessProvider.execute, ClaudeHeadlessProvider.command_argv

    def run(self, *args: Any, **kwargs) -> Any:
        provider = replace(self, budget_seconds=request["config"]["provider_seconds"], prompt=rendered, model="opus")
        if kwargs.get("execution") is not None:
            kwargs["execution"] = ObservedExecution(kwargs["execution"], job, request, protected)
        return execute(provider, *args, **kwargs)

    def command(self, *args: Any, **kwargs) -> Any:
        result = argv(self, *args, **kwargs)
        result[result.index("--tools") + 1] = "Read,Grep,Glob,Write,Bash,WebSearch"
        root = Path(args[0] if args else kwargs["working_path"])
        return [*result, "--settings", str(root / "tool-settings.json"), "--setting-sources", "", "--effort", "medium"]

    ClaudeHeadlessProvider.execute, ClaudeHeadlessProvider.command_argv = run, command
    try:
        yield
    finally:
        ClaudeHeadlessProvider.execute, ClaudeHeadlessProvider.command_argv = execute, argv


def stage(root, request) -> Any:
    """Materialize the installed package, dependencies and retained source files."""
    from ._engine.documents import materialize
    from ._engine.sources import model_input

    source = Path(__file__).parent
    shutil.copytree(source, root / "shepherd_citation_checker", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    with ZipFile(source.parent / "dependencies.zip") as bundle:
        bundle.extractall(root)
    for item in request["evidence"]:
        if digest(item["source"]) != item["sha256"]:
            raise ValueError("Frozen evidence changed before review")
        target = root / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item["source"], target)
    index = materialize(root)
    packet = model_input(request["references"], root, request["config"]["searches"] > 0)
    packed = {s["source_id"]: s for s in packet["sources"]}
    packet["sources"] = [{**row, **packed.get(row["source_id"], {})} for row in index]
    packet["source_index"] = "source-index.json"
    packet["discovery_budget"] = {k: request["config"][k] for k in ("searches", "fetches")}
    write(root / "input.json", packet)
    write(root / "adjudication-limits.json", {k: request["config"][k] for k in ("searches", "fetches")})
    hook = {"type": "command", "command": "python3 -B -m shepherd_citation_checker._engine.hooks", "timeout": 10}
    write(
        root / "tool-settings.json",
        {
            "hooks": {
                "PreToolUse": [{"matcher": ".*", "hooks": [hook]}],
                "PostToolUse": [{"matcher": "WebSearch|Write", "hooks": [hook]}],
            }
        },
    )
    return packet


def worker(job) -> Any:
    """Run one jailed review and finalize every captured citation independently."""
    from click.testing import CliRunner
    from shepherd.cli import main as cli

    import shepherd as sp

    from .agent_task import review_citations
    from .reviewer import prompt

    request = read(job / "request.json")
    receipt = {
        "job": job.name,
        "ids": [r["id"] for r in request["references"]],
        "status": "failed",
        "batch_path": str(job),
        "phase": "review",
        "timing_seconds": {},
    }
    started = time.monotonic()
    try:
        auth_check()
        root = Path(tempfile.mkdtemp(prefix="shepherd-citation-")) / "workspace"
        root.mkdir()
        receipt["runtime_directory"] = str(root.parent)
        packet = stage(root, request)
        rendered = prompt(packet, CheckerConfig(**request["config"]))
        (job / "review-prompt.txt").write_text(rendered)
        write(job / "model-input.json", packet)
        receipt["prompt_sha256"] = digest(job / "review-prompt.txt")
        protected = {
            p.relative_to(root).as_posix(): digest(p)
            for p in root.rglob("*")
            if p.is_file() and p.name != "source-index.json"
        }
        receipt["timing_seconds"]["input_preparation"] = round(time.monotonic() - started, 3)
        step = time.monotonic()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        result = CliRunner().invoke(cli, ["init", str(root)])
        if result.exit_code:
            raise RuntimeError(result.output)
        with sp.open(root) as workspace, provider_adapter(job, request, protected, rendered):
            workspace.tasks.register(review_citations)
            receipt["timing_seconds"]["workspace_initialization"] = round(time.monotonic() - step, 3)
            step = time.monotonic()
            run = workspace.run(
                review_citations,
                repo=workspace.git_repo(),
                instructions=rendered,
                placement="jail",
                runtime={"provider": "claude", "model": "opus"},
            )
            receipt["timing_seconds"]["shepherd_run"] = round(time.monotonic() - step, 3)
            receipt.update(run_ref=run.run_ref, runtime_status="completed")
            write(job / "trace.json", workspace.runs.trace(run.run_ref, events=True).payload)
            write(job / "run.json", workspace.runs.show(run.run_ref).to_json())
            exported = job / "review"
            for directory in ("evidence", "documents", "search-results"):
                if (root / directory).exists():
                    shutil.copytree(root / directory, exported / directory)
            shutil.copyfile(root / "source-index.json", exported / "source-index.json")
            # The checkpoint observer retains output writes before transport failure;
            # validate its unchanged-input seal before accepting any result.
    except Exception as exc:  # noqa: BLE001 -- preserve failure receipts at the process/tool boundary
        receipt.update(runtime_status="failed", error=f"{type(exc).__name__}: {exc}")
        receipt["authentication_failure"] = any(
            v in str(exc).lower() for v in ("authentication", "oauth", "subscription login")
        )
    try:
        finalize(job, request, receipt)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        receipt["checkpoint_error"] = f"{type(exc).__name__}: {exc}"
    receipt["elapsed_seconds"] = round(time.monotonic() - started, 3)
    write(job / "receipt.json", receipt)


def launch(job, code) -> Any:
    """Launch the frozen worker with a hard deadline and durable receipt."""
    if (job / "receipt.json").exists():
        return read(job / "receipt.json")
    request = read(job / "request.json")
    active = job / "active.json"
    if active.exists():
        old = read(active)
        proc = Path(f"/proc/{old['pid']}/cmdline")
        if proc.exists() and str(job).encode() in proc.read_bytes():
            raise RuntimeError("The citation worker is still running")
        return failed_receipt(job, request, "Interrupted worker without a completion receipt", None)
    try:
        auth_check()
    except RuntimeError as exc:
        receipt = failed_receipt(job, request, str(exc), 0.0)
        receipt["authentication_failure"] = "authentication" in str(exc).lower()
        write(job / "receipt.json", receipt)
        return receipt
    started = time.monotonic()
    env = {**os.environ, "PYTHONPATH": str(code)}
    with (job / "process.log").open("w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "shepherd_citation_checker.runtime", str(job)],
            cwd=code,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        write(active, {"pid": proc.pid, "started_at": datetime.now(timezone.utc).isoformat()})
        try:
            proc.wait(timeout=request["config"]["worker_seconds"])
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    if not (job / "receipt.json").exists():
        return failed_receipt(
            job, request, "Worker stopped without a receipt; inspect process.log", round(time.monotonic() - started, 3)
        )
    return read(job / "receipt.json")


def failed_receipt(job, request, reason, seconds) -> Any:
    """Retain a runtime failure and recover any sealed completed outputs."""
    receipt = {
        "job": job.name,
        "ids": [r["id"] for r in request["references"]],
        "status": "failed",
        "batch_path": str(job),
        "elapsed_seconds": seconds,
        "error": reason,
    }
    try:
        recover_hard_stop(job, request)
        finalize(job, request, receipt)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        receipt["checkpoint_error"] = str(exc)
    write(job / "receipt.json", receipt)
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", type=Path)
    worker(parser.parse_args().job.resolve())
