"""Visual-artifact notebook helpers backed by Shepherd workspace-control APIs."""

# ruff: noqa: D103

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shepherd_dialect.run_driver import ShepherdRunDriver
from shepherd_dialect.workspace_control import (
    RUN_ARTIFACT_INPUT_SCHEMA,
    Flow,
    RunArtifactInputRef,
    ShepherdRunLedgerDriver,
    ShepherdTaskArtifactDriver,
    ShepherdTaskLedgerDriver,
    ShepherdWorkspace,
    WorkspaceRun,
    get_run_args,
)
from shepherd_dialect.workspace_control.feature_flags import _seal_and_select_enabled
from vcs_core import FilesystemSubstrate, MarkerSubstrate, Store, VcsCore, build_builtin_substrate_context
from vcs_core.runtime_api import native_jail_available
from vcs_core.runtime_substrate import TaskTraceSubstrateDriver
from vcs_core.substrates import detect_overlay_backend

from .recovery_core import AMENDMENT, PLAN_PATH, classify_failure, make_plan
from .tasks import LIVE_ARTIFACT_TASK_REF, LIVE_REVIEW_TASK_REF, STATIC_ARTIFACT_TASK_REF
from .tile import (
    ARTIFACT_PATH,
    DEFAULT_MODEL,
    DEFAULT_STRATEGIES,
    REQUEST,
    TileBrief,
    load_brief,
    plant_defect,
    render_static_tile,
    static_review_verdicts,
)

TASK_REF = STATIC_ARTIFACT_TASK_REF
VERDICT_PATH = "verdict.json"
DECISION_PATH = "decision.json"
DIAGNOSIS_PATH = "diagnosis.json"
MINIMUM_PYTHON = (3, 11)
_RESERVED_ARTIFACT_REF_NAMES = frozenset(
    {
        "artifact_path",
        "artifact_text",
        "output_content",
        "output_path",
        "output_text",
        "runtime",
    }
)


class NotebookSetupError(RuntimeError):
    """Raised when a public notebook is not running from a usable environment."""


@dataclass(frozen=True)
class LaunchWorkspace:
    """Notebook-owned Shepherd workspace plus one flow."""

    control: ShepherdWorkspace
    flow: Flow
    repo: Any
    root: Path

    def close(self) -> None:
        self.control.close()


@dataclass(frozen=True)
class StudioSelection:
    """Selection result read from a retained reviewer output."""

    reviewer: WorkspaceRun
    candidates: tuple[Mapping[str, object],...]
    selected: str

    @property
    def failed(self) -> tuple[str,...]:
        return tuple(str(item["id"]) for item in self.candidates if item.get("verdict") == "fail")


@dataclass(frozen=True)
class GradedRun:
    """One right-sizing evaluator result."""

    run: WorkspaceRun
    config: str
    model: str
    cost: str
    passed: bool
    catches_hard_fail: bool


@dataclass(frozen=True)
class ClaudePreflight:
    """Local live-Claude readiness for the optional UC1 path."""

    ready: bool
    reason: str


def bootstrap(*, example_root: str | Path | None = None) -> None:
    """Validate that the notebook is running against this checked-out example bundle."""
    _require_python()
    _require_example_root(example_root)
    _require_importable("shepherd_dialect", "Shepherd workspace-control")
    _require_importable("vcs_core", "VcsCore retained-output custody")
    _require_importable("IPython", "notebook display")
    _require_overlay_backend()


def default_prompt() -> str:
    return REQUEST


def prompt_to_brief(prompt: str) -> TileBrief:
    base = load_brief()
    return TileBrief(
        name=base.name,
        request=prompt,
        required_labels=base.required_labels,
        format=base.format,
    )


def open_workspace(
    name: str, *, prompt: str | None = None, metadata: Mapping[str, object] | None = None
) -> LaunchWorkspace:
    """Create a temporary Shepherd workspace and open one flow."""
    root = Path(tempfile.mkdtemp(prefix=f"shepherd-{_slug(name)}-"))
    root.mkdir(parents=True, exist_ok=True)
    store = Store(str(root / ".vcscore"))
    # Pick a copy-on-write carrier for the isolated run scopes: kernel/fuse overlay on
    # Linux, APFS clonefile on macOS. Auto-detect so the notebooks run on any platform
    # with a usable backend instead of hardcoding the macOS carrier.
    backend = detect_overlay_backend()
    if backend is None and sys.platform == "darwin":
        backend = "clonefile"
    config = {"backend": backend} if backend else None
    context = build_builtin_substrate_context(store=store, workspace=root, config=config)
    mg = VcsCore(
        str(root),
        substrates=[
            MarkerSubstrate(context),
            FilesystemSubstrate(context),
            TaskTraceSubstrateDriver(),
            ShepherdTaskLedgerDriver(),
            ShepherdTaskArtifactDriver(),
            ShepherdRunLedgerDriver(),
            ShepherdRunDriver(),
        ],
        store=store,
    )
    with _seal_and_select_enabled():
        mg.activate()
        if prompt is not None:
            mg.exec(
                "filesystem",
                "write",
                scope=mg.ground,
                path="brief.json",
                content=(json.dumps({"prompt": prompt}, indent=2, sort_keys=True) + "\n").encode(),
            )
    control = ShepherdWorkspace(
        mg,
        trace_store_path=root / ".vcscore" / "shepherd" / "trace.sqlite",
        workspace_path=root,
    )
    control.tasks.register(TASK_REF, may_default="ReadWrite")
    control.tasks.register(LIVE_ARTIFACT_TASK_REF, may_default="ReadWrite")
    control.tasks.register(LIVE_REVIEW_TASK_REF, may_default="ReadWrite")
    flow = control.flows.open(name=name, metadata=dict(metadata or {}))
    return LaunchWorkspace(control=control, flow=flow, repo=control.git_repo(), root=root)


def run_static(
    workspace: LaunchWorkspace,
    *,
    name: str,
    output_path: str,
    output_text: str | None = None,
    output_content: object | None = None,
    after: Sequence[WorkspaceRun] = (),
    metadata: Mapping[str, object] | None = None,
    model: str | None = None,
) -> WorkspaceRun:
    """Run one deterministic static provider task through public flow/run APIs."""
    args: dict[str, object] = {"output_path": output_path}
    if output_text is not None:
        args["output_text"] = output_text
    elif output_content is not None:
        args["output_content"] = output_content
    return workspace.flow.fork(
        TASK_REF,
        repo=workspace.repo,
        name=name,
        args=args,
        after=after,
        runtime=_static_runtime(model=model),
        placement="advisory",
        metadata=metadata,
    )


def run_with_artifact_ref(
    workspace: LaunchWorkspace,
    *,
    name: str,
    output_path: str,
    ref_name: str,
    artifact_ref: object,
    output_text: str | None = None,
    output_content: object | None = None,
    after: Sequence[WorkspaceRun] = (),
    metadata: Mapping[str, object] | None = None,
) -> WorkspaceRun:
    """Run a static task while preserving one durable artifact citation."""
    return run_with_artifact_refs(
        workspace,
        name=name,
        output_path=output_path,
        refs={ref_name: artifact_ref},
        output_text=output_text,
        output_content=output_content,
        after=after,
        metadata=metadata,
    )


def run_with_artifact_refs(
    workspace: LaunchWorkspace,
    *,
    name: str,
    output_path: str,
    refs: Mapping[str, object],
    output_text: str | None = None,
    output_content: object | None = None,
    after: Sequence[WorkspaceRun] = (),
    metadata: Mapping[str, object] | None = None,
) -> WorkspaceRun:
    """Run a static task while preserving multiple durable artifact citations."""
    args: dict[str, object] = {"output_path": output_path, **_validated_artifact_refs(refs)}
    if output_text is not None:
        args["output_text"] = output_text
    elif output_content is not None:
        args["output_content"] = output_content
    return workspace.flow.fork(
        TASK_REF,
        repo=workspace.repo,
        name=name,
        args=args,
        after=after,
        runtime=_static_runtime(),
        placement="advisory",
        metadata=metadata,
    )


def claude_preflight() -> ClaudePreflight:
    """Return readiness for the optional local Claude lane."""
    if shutil.which("claude") is None:
        return ClaudePreflight(False, "the `claude` CLI is not on PATH")
    if not native_jail_available():
        return ClaudePreflight(False, "native jail support is not available on this host")
    return ClaudePreflight(
        True,
        "local Claude CLI and native jail support are available; live runs still require Claude auth "
        "visible to the redirected jailed CLI environment",
    )


def require_claude() -> None:
    """Raise a notebook-friendly setup error unless live Claude can run."""
    preflight = claude_preflight()
    if not preflight.ready:
        raise NotebookSetupError(f"Live Claude mode is unavailable: {preflight.reason}.")


def run_claude_artifact(
    workspace: LaunchWorkspace,
    *,
    name: str,
    prompt: str,
    variant: str,
    instruction: str,
    output_path: str = ARTIFACT_PATH,
    after: Sequence[WorkspaceRun] = (),
    metadata: Mapping[str, object] | None = None,
    model: str | None = None,
) -> WorkspaceRun:
    """Run one live Claude artifact attempt through public flow/run APIs."""
    return workspace.flow.fork(
        LIVE_ARTIFACT_TASK_REF,
        repo=workspace.repo,
        name=name,
        args={
            "prompt": prompt,
            "variant": variant,
            "instruction": instruction,
            "output_path": output_path,
        },
        after=after,
        runtime=_claude_runtime(model=model),
        placement="auto",
        metadata=metadata,
    )


def run_claude_review(
    workspace: LaunchWorkspace,
    *,
    name: str,
    prompt: str,
    refs: Mapping[str, object],
    output_path: str = VERDICT_PATH,
    after: Sequence[WorkspaceRun] = (),
    metadata: Mapping[str, object] | None = None,
    model: str | None = None,
) -> WorkspaceRun:
    """Run one live Claude reviewer over explicit retained artifact refs."""
    return workspace.flow.fork(
        LIVE_REVIEW_TASK_REF,
        repo=workspace.repo,
        name=name,
        args={"prompt": prompt, "output_path": output_path, **_validated_artifact_refs(refs)},
        after=after,
        runtime=_claude_runtime(model=model),
        placement="auto",
        metadata=metadata,
    )


def read(run: WorkspaceRun, path: str = ARTIFACT_PATH) -> str:
    return run.output().read_text(path)


def read_json(run: WorkspaceRun, path: str) -> object:
    return run.output().read_json(path)


def artifact(run: WorkspaceRun, path: str = ARTIFACT_PATH) -> object:
    return run.output().artifact(path)


def artifact_ref(run: WorkspaceRun, path: str = ARTIFACT_PATH, *, label: str | None = None) -> object:
    return run.output().artifact(path).to_input(label=label)


def changed_paths(run: WorkspaceRun) -> tuple[str,...]:
    return run.output().changed_paths


def run_record(workspace: LaunchWorkspace, run: WorkspaceRun) -> object:
    record = workspace.control.runs.show(run.run_ref)
    if record is None:
        raise LookupError(f"unknown run: {run.run_ref}")
    return record


def run_args(workspace: LaunchWorkspace, run: WorkspaceRun) -> Mapping[str, object]:
    record = run_record(workspace, run)
    args_ref = getattr(record, "args_ref", None)
    if not isinstance(args_ref, str) or not args_ref:
        raise LookupError(f"run has no retained args: {run.run_ref}")
    args = get_run_args(workspace.control.mg, args_ref)
    if not isinstance(args, Mapping):
        raise TypeError(f"run args payload is malformed: {run.run_ref}")
    return args


def variant_prompts() -> dict[str, str]:
    return {
        "contour-map": "Use a contour-map visual with the update path descending toward the minimum.",
        "uphill-path": "Use an update-path visual that moves uphill away from the minimum.",
    }


def variant_html(prompt: str, variant: str, *, model: str | None = None) -> str:
    brief = prompt_to_brief(prompt)
    html = render_static_tile(brief.to_payload(), variant, model=model)
    return plant_defect(html, variant, True)


def review_content(prompt: str, attempts: Mapping[str, WorkspaceRun]) -> dict[str, object]:
    brief = prompt_to_brief(prompt)
    candidates = [{"id": name, "html": read(run)} for name, run in attempts.items()]
    return static_review_verdicts(candidates, brief)


def selection_from_review(reviewer: WorkspaceRun) -> StudioSelection:
    payload = reviewer.output().read_json(VERDICT_PATH)
    if not isinstance(payload, Mapping):
        raise TypeError("review verdict must be a JSON object")
    candidates = payload.get("candidates", [])
    if not isinstance(candidates, list):
        raise TypeError("review verdict candidates must be a list")
    return StudioSelection(
        reviewer=reviewer,
        candidates=tuple(item for item in candidates if isinstance(item, Mapping)),
        selected=str(payload["selected"]),
    )


def model_choices() -> dict[str, str]:
    return {"high": "opus", "mid": "sonnet", "cheap": "haiku"}


def model_cost(model: str) -> str:
    return {"opus": "high", "sonnet": "medium", "haiku": "low"}.get(model, "unknown")


def evaluator_content(config_name: str, model: str) -> dict[str, object]:
    catches_hard_fail = model != "haiku"
    return {
        "config": config_name,
        "model": model,
        "cost": model_cost(model),
        "hard_fail_catch": catches_hard_fail,
        "passed": catches_hard_fail,
    }


def grade_runs(runs: Mapping[str, WorkspaceRun]) -> dict[str, GradedRun]:
    graded: dict[str, GradedRun] = {}
    for config, run in runs.items():
        payload = run.output().read_json("verdict.json")
        if not isinstance(payload, Mapping):
            raise TypeError("evaluator verdict must be a JSON object")
        graded[config] = GradedRun(
            run=run,
            config=config,
            model=str(payload["model"]),
            cost=str(payload["cost"]),
            passed=bool(payload["passed"]),
            catches_hard_fail=bool(payload["hard_fail_catch"]),
        )
    return graded


def selector_content(graded: Mapping[str, GradedRun]) -> dict[str, object]:
    passing = [item for item in graded.values() if item.passed]
    cost_rank = {"low": 0, "medium": 1, "high": 2}
    selected = min(passing, key=lambda item: cost_rank.get(item.cost, 99))
    return {
        "kept": selected.config,
        "passed": [item.config for item in passing],
        "dropped": [item.config for item in graded.values() if item.config != selected.config],
    }


def plan_for(prompt: str) -> tuple[TileBrief, dict[str, object]]:
    brief = prompt_to_brief(prompt)
    return brief, make_plan(brief)


def draft_html(brief: TileBrief, *, corrupt: bool) -> str:
    html = render_static_tile(brief.to_payload(), "draft_v1" if corrupt else "draft_retry")
    return plant_defect(html, "uphill-path" if corrupt else "contour-map", corrupt)


def diagnosis_content(issues: Sequence[str]) -> dict[str, object]:
    diagnosis = classify_failure(issues)
    return {
        "failure_type": diagnosis.failure_type,
        "bad_step": diagnosis.bad_step,
        "evidence": diagnosis.evidence,
        "retry_boundary": diagnosis.retry_boundary,
        "recommended_change": diagnosis.recommended_change,
    }


def retry_amendment() -> str:
    return AMENDMENT


def _static_runtime(*, model: str | None = None) -> dict[str, object]:
    runtime: dict[str, object] = {"provider": "static"}
    if model is not None:
        runtime["model"] = model
    return runtime


def _claude_runtime(*, model: str | None = None) -> dict[str, object]:
    runtime: dict[str, object] = {"provider": "claude"}
    if model is not None:
        runtime["model"] = model
    return runtime


def _require_python() -> None:
    if sys.version_info < MINIMUM_PYTHON:
        current = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        required = ".".join(str(part) for part in MINIMUM_PYTHON)
        raise NotebookSetupError(
            f"Python {required}+ is required for these notebooks; current kernel is Python {current}. "
            "Launch from the repository root with `make notebooks`."
        )


def _require_example_root(example_root: str | Path | None) -> Path:
    root = Path(example_root).expanduser().resolve() if example_root is not None else Path(__file__).resolve().parents[2]
    expected = root / "shepherd_usecases" / "visual_artifact" / "launch.py"
    actual = Path(__file__).resolve()
    if not expected.exists():
        raise NotebookSetupError(
            "Cannot find the visual-artifact example package. Launch JupyterLab from the repository root with "
            "`make notebooks`, or add examples/notebooks/visual_artifact to sys.path."
        )
    if expected.resolve() != actual:
        raise NotebookSetupError(
            f"Imported visual-artifact helpers from {actual}, but the notebook resolved example root {root}. "
            "Restart the kernel and launch from the repository root with `make notebooks`."
        )
    return root


def _require_importable(module_name: str, label: str) -> None:
    if importlib.util.find_spec(module_name) is None:
        raise NotebookSetupError(
            f"{label} is not importable in this kernel. Launch from the repository root with `make notebooks`."
        )


def _require_overlay_backend() -> None:
    """Each run forks an isolated, reversible scope, which needs a copy-on-write carrier.

    macOS uses the APFS clonefile carrier automatically. Linux uses a kernel or FUSE
    overlay; unprivileged containers need fuse-overlayfs. Fail early with an actionable
    message instead of the low-level "no overlay backend is available" raised mid-run.
    """
    if detect_overlay_backend() is not None or sys.platform == "darwin":
        return
    raise NotebookSetupError(
        "These notebooks fork an isolated, reversible workspace scope per run, which needs a "
        "copy-on-write overlay backend. None is available on this Linux host. Install FUSE "
        "overlayfs (Debian/Ubuntu: `sudo apt-get install -y fuse-overlayfs`), then restart the "
        "kernel. `make notebooks` checks this for you."
    )


def _validated_artifact_refs(refs: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(refs, Mapping) or not refs:
        raise ValueError("artifact refs must be a non-empty mapping")
    validated: dict[str, object] = {}
    for name, value in refs.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("artifact ref names must be non-empty strings")
        if name in _RESERVED_ARTIFACT_REF_NAMES or name.startswith(""):
            raise ValueError(f"artifact ref name is reserved: {name!r}")
        validated[name] = _validated_artifact_ref_value(name, value)
    return validated


def _validated_artifact_ref_value(name: str, value: object) -> RunArtifactInputRef:
    if isinstance(value, RunArtifactInputRef):
        return value
    if isinstance(value, Mapping):
        if value.get("kind") != RUN_ARTIFACT_INPUT_SCHEMA:
            raise ValueError(f"artifact ref value for {name!r} must be an artifact reference")
        try:
            return RunArtifactInputRef.from_json(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"artifact ref value for {name!r} is malformed") from exc
    raise ValueError(f"artifact ref value for {name!r} must be an artifact reference")


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in value.lower()).strip("-") or "workspace"


__all__ = [
    "ARTIFACT_PATH",
    "DECISION_PATH",
    "DEFAULT_MODEL",
    "DEFAULT_STRATEGIES",
    "DIAGNOSIS_PATH",
    "MINIMUM_PYTHON",
    "PLAN_PATH",
    "TASK_REF",
    "VERDICT_PATH",
    "ClaudePreflight",
    "GradedRun",
    "LaunchWorkspace",
    "NotebookSetupError",
    "StudioSelection",
    "TileBrief",
    "artifact",
    "artifact_ref",
    "bootstrap",
    "changed_paths",
    "claude_preflight",
    "default_prompt",
    "diagnosis_content",
    "draft_html",
    "evaluator_content",
    "grade_runs",
    "model_choices",
    "open_workspace",
    "plan_for",
    "prompt_to_brief",
    "read",
    "read_json",
    "require_claude",
    "retry_amendment",
    "review_content",
    "run_args",
    "run_claude_artifact",
    "run_claude_review",
    "run_record",
    "run_static",
    "run_with_artifact_ref",
    "run_with_artifact_refs",
    "selection_from_review",
    "selector_content",
    "variant_html",
    "variant_prompts",
]
