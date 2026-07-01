"""v0.1.1 workspace-control static runtime provider coverage."""

from __future__ import annotations

import sys
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from vcs_core import FilesystemSubstrate, MarkerSubstrate, Store, VcsCore, build_builtin_substrate_context
from vcs_core.runtime_api import native_jail_available
from vcs_core.runtime_substrate import TaskTraceSubstrateDriver

import shepherd_dialect.workspace_control.runtime_provider as runtime_provider_module
from shepherd_dialect.provider_runtime import ExecutionProviderResult, ProviderEvent
from shepherd_dialect.run_driver import ShepherdRunDriver
from shepherd_dialect.workspace_control import (
    RunStartError,
    ShepherdRunLedgerDriver,
    ShepherdTaskArtifactDriver,
    ShepherdTaskLedgerDriver,
    ShepherdWorkspace,
    WorkspaceControlError,
    get_run_args,
)
from shepherd_dialect.workspace_control.feature_flags import _seal_and_select_enabled
from shepherd_dialect.workspace_control.runtime_provider import (
    ClaudeWorkspaceRuntimeProvider,
    WorkspaceRuntimeInputArtifact,
)
from shepherd_dialect.workspace_control.schemas import TaskArtifactLock, TaskArtifactRef

if TYPE_CHECKING:
    from pathlib import Path

    from shepherd_runtime.nucleus import GitRepo


def _make_workspace(root: Path) -> ShepherdWorkspace:
    root.mkdir(parents=True, exist_ok=True)
    store = Store(str(root / ".vcscore"))
    context = build_builtin_substrate_context(store=store, workspace=root, config={"backend": "clonefile"})
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
    return ShepherdWorkspace(
        mg,
        trace_store_path=root / ".vcscore" / "shepherd" / "trace.sqlite",
        workspace_path=root,
    )


def _write_static_probe_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    module_path = tmp_path / "runtime_provider_tasks.py"
    module_path.write_text(
        """
def generate(repo, **kwargs):
    raise AssertionError("static runtime provider should own execution")
""",
        encoding="utf-8",
    )
    sys.modules.pop("runtime_provider_tasks", None)
    monkeypatch.syspath_prepend(str(tmp_path))
    return "runtime_provider_tasks:generate"


def _seed_selected_workspace(workspace: ShepherdWorkspace) -> GitRepo:
    with _seal_and_select_enabled():
        workspace.mg.exec("filesystem", "write", scope=workspace.mg.ground, path="base.txt", content=b"base\n")
    return workspace.git_repo()


def _test_task_lock() -> TaskArtifactLock:
    digest = "sha256:" + ("1" * 64)
    return TaskArtifactLock(
        task_id="runtime_provider_tasks.generate",
        version="v1",
        artifact_ref=TaskArtifactRef(
            binding="task",
            store_id="store",
            resource_id="resource",
            head="head",
            artifact_digest=digest,
        ),
        artifact_digest=digest,
        schema_digest="sha256:" + ("2" * 64),
    )


def test_workspace_discover_forwards_explicit_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vcs_core

    root = tmp_path / "ws"
    root.mkdir(parents=True)
    real_build_context = vcs_core.build_builtin_substrate_context
    configs: list[dict[str, object]] = []

    def capture_context(
        store: object,
        *,
        workspace: Path | None = None,
        config: dict[str, object] | None = None,
    ) -> object:
        configs.append(dict(config or {}))
        return real_build_context(store, workspace=workspace, config=config)

    monkeypatch.setattr(vcs_core, "build_builtin_substrate_context", capture_context)
    (root / ".vcscore").mkdir()

    workspace = ShepherdWorkspace.discover(root, activate=False, backend="fuse")
    try:
        assert configs == [{"backend": "fuse"}]
    finally:
        workspace.close()


def test_workspace_discover_can_delegate_backend_autodetect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vcs_core

    root = tmp_path / "ws"
    root.mkdir(parents=True)
    real_build_context = vcs_core.build_builtin_substrate_context
    configs: list[dict[str, object]] = []

    def capture_context(
        store: object,
        *,
        workspace: Path | None = None,
        config: dict[str, object] | None = None,
    ) -> object:
        configs.append(dict(config or {}))
        return real_build_context(store, workspace=workspace, config=config)

    monkeypatch.setattr(vcs_core, "build_builtin_substrate_context", capture_context)
    (root / ".vcscore").mkdir()

    workspace = ShepherdWorkspace.discover(root, activate=False, backend=None)
    try:
        assert configs == [{}]
    finally:
        workspace.close()


def test_claude_workspace_runtime_provider_records_events_and_scrubs_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _FakeClaudeProvider:
        def __init__(self, invocation: object) -> None:
            captured["provider_id"] = invocation.provider_id
            captured["prompt"] = invocation.prompt
            captured["model"] = invocation.model_name
            captured["task_id"] = invocation.task_lock.task_id
            captured["input_count"] = len(invocation.input_artifacts)
            self.provider_id = invocation.provider_id
            self.model = invocation.model_name

        def execute(
            self,
            task_body: object,
            stack: object,
            context: object,
            args: object,
            *,
            execution: object,
            confinement: object,
        ) -> ExecutionProviderResult:
            del task_body, stack, context, args, confinement
            proc = execution.launch_confined(["fake-claude"], object())
            assert proc.returncode == 0
            event = ProviderEvent(
                kind="provider.invocation.completed",
                provider_id=self.provider_id,
                invocation_id="claude:fake-scope",
                sequence=0,
                event_id="claude:fake-scope:completed",
                model=self.model,
                payload={"transport": "fake"},
            )
            return ExecutionProviderResult(outcome={"status": "ok"}, provider_events=(event,))

    class _FakeExecution:
        working_path = tmp_path
        identity = SimpleNamespace(scope_instance_id="fake-scope", scope_name="fake")

        def launch_confined(self, command: list[str], confinement: object) -> object:
            del command, confinement
            (tmp_path / "index.html").write_text("<!doctype html><title>Claude fake</title>", encoding="utf-8")
            scratch = tmp_path / ".claude-scratch"
            scratch.mkdir()
            (scratch / "transcript.jsonl").write_text("private\n", encoding="utf-8")
            (tmp_path / ".claude-sdk-scratch").write_text("private\n", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        runtime_provider_module,
        "_WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS",
        SimpleNamespace(claude=_FakeClaudeProvider),
    )
    metadata: dict[str, object] = {"launch_confined_attempted": False}
    provider = ClaudeWorkspaceRuntimeProvider(
        task_lock=_test_task_lock(),
        artifact_payload={
            "entrypoint": {"module": "runtime_provider_tasks", "qualname": "generate"},
            "files": [
                {
                    "path": "runtime_provider_tasks.py",
                    "content_encoding": "utf-8",
                    "content": "def generate(repo, *, output_path):\n '''Write an HTML artifact.'''\n",
                }
            ],
        },
        kwargs={"output_path": "index.html", "source": {"kind": "skeleton.run_artifact_input.v1"}},
        model_name="sonnet",
        input_artifacts=(
            WorkspaceRuntimeInputArtifact(
                source_run_ref="run-source",
                source_output_id="output-source",
                source_output_name="workspace",
                source_binding="workspace",
                source_path="candidate/index.html",
                materialized_path=".shepherd-inputs/01-candidate/candidate/index.html",
                content=b"candidate html",
                label="candidate",
                content_digest="sha256:" + ("3" * 64),
            ),
        ),
        launch_metadata=metadata,
    )

    result = provider.execute(None, None, None, {}, execution=_FakeExecution(), confinement=object())

    assert result.outcome == {"status": "ok"}
    assert (tmp_path / "index.html").read_text(encoding="utf-8").startswith("<!doctype html>")
    assert not (tmp_path / ".shepherd-inputs").exists()
    assert not (tmp_path / ".claude-scratch").exists()
    assert not (tmp_path / ".claude-sdk-scratch").exists()
    assert metadata["launch_confined_attempted"] is True
    assert metadata["provider_events"] == [result.provider_events[0].stable_payload()]
    assert isinstance(metadata["provider_prompt_digest"], str)
    assert metadata["provider_private_dirs"] == [".shepherd-inputs", ".claude-scratch", ".claude-sdk-scratch"]
    assert metadata["provider_input_manifest"] == [
        {
            "source_run_ref": "run-source",
            "source_output_id": "output-source",
            "source_output_name": "workspace",
            "source_binding": "workspace",
            "source_path": "candidate/index.html",
            "materialized_path": ".shepherd-inputs/01-candidate/candidate/index.html",
            "byte_length": len(b"candidate html"),
            "label": "candidate",
            "content_digest": "sha256:" + ("3" * 64),
        }
    ]
    assert captured["provider_id"] == "claude"
    assert captured["model"] == "sonnet"
    assert captured["task_id"] == "runtime_provider_tasks.generate"
    assert captured["input_count"] == 1
    prompt = captured["prompt"]
    assert isinstance(prompt, str)
    assert "Task id: runtime_provider_tasks.generate" in prompt
    assert ".shepherd-inputs/01-candidate/candidate/index.html" in prompt


def test_workspace_task_run_static_runtime_provider_publishes_retained_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_static_probe_task(tmp_path, monkeypatch)
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)

        run = workspace.tasks.task("runtime_provider_tasks.generate").run(
            repo=repo,
            args={
                "output_path": "index.html",
                "output_text": "<!doctype html><title>static provider</title>",
            },
            placement="advisory",
            runtime={"provider": "static", "model": "fixture-v1"},
        )

        assert run.output().read_file("index.html") == (
            b"<!doctype html><title>static provider</title>",
            0o100644,
        )
        record = workspace.runs.show(run.run_ref)
        assert record is not None
        assert record.status == "retained"
        assert record.enforcement == "advisory"
        assert record.execution_evidence.requested_placement == "advisory"
        assert record.execution_evidence.resolved_placement == "advisory"
        assert record.execution_evidence.execution_descriptor is not None
        assert record.execution_evidence.execution_descriptor["provider"] == "static"
        execution = record.task_executions[0]
        assert execution.executor_kind == "in_process"
        assert execution.executor_policy == "provider_runtime"
        assert execution.metadata["runtime_provider"] == "static"
        assert execution.metadata["runtime_model"] == "fixture-v1"
        assert execution.metadata["launch_confined_attempted"] is False
        assert [event["kind"] for event in execution.metadata["provider_events"]] == [
            "provider.invocation.started",
            "provider.invocation.completed",
        ]
        policy = record.launch_context.settlement_policy
        assert policy is not None
        assert policy["runtime"] == {
            "requested": {"provider": {"id": "static"}, "model": {"name": "fixture-v1"}},
            "resolved": {"provider": "static", "model": "fixture-v1"},
        }
        assert policy["execution_enforcement"]["provider"] == "static"
    finally:
        workspace.close()


def test_workspace_run_static_runtime_persists_args_and_artifact_input_refs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_static_probe_task(tmp_path, monkeypatch)
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)

        producer = workspace.run(
            "runtime_provider_tasks.generate",
            repo=repo,
            args={"output_path": "data.json", "output_content": {"selected": True}},
            placement="advisory",
            runtime={"provider": "static"},
        )
        output = producer.output()
        assert output.read_text("data.json") == '{\n "selected": true\n}'
        assert output.read_json("data.json") == {"selected": True}

        artifact_ref = output.artifact("data.json").to_input(label="candidate")
        consumer = workspace.run(
            "runtime_provider_tasks.generate",
            repo=repo,
            args={
                "source": artifact_ref,
                "output_path": "review.json",
                "output_content": {"winner": "candidate"},
            },
            placement="advisory",
            runtime={"provider": "static"},
        )

        record = workspace.runs.show(consumer.run_ref)
        assert record is not None
        assert record.args_ref is not None
        args_payload = get_run_args(workspace.mg, record.args_ref)
        assert args_payload is not None
        assert args_payload["run_ref"] == consumer.run_ref
        assert args_payload["args_digest"] == record.args_digest
        persisted = args_payload["payload"]
        assert isinstance(persisted, dict)
        assert persisted["source"] == artifact_ref.to_json()
        assert args_payload["input_refs"] == [artifact_ref.to_json()]

        json_consumer = workspace.run(
            "runtime_provider_tasks.generate",
            repo=repo,
            args={
                "source": artifact_ref.to_json(),
                "output_path": "review.json",
                "output_content": {"winner": "candidate"},
            },
            placement="advisory",
            runtime={"provider": "static"},
        )
        json_record = workspace.runs.show(json_consumer.run_ref)
        assert json_record is not None
        assert json_record.args_digest == record.args_digest
        assert json_record.args_ref is not None
        json_args_payload = get_run_args(workspace.mg, json_record.args_ref)
        assert json_args_payload is not None
        assert json_args_payload["payload"] == args_payload["payload"]
        assert json_args_payload["payload_digest"] == args_payload["payload_digest"]
    finally:
        workspace.close()


def test_workspace_run_rejects_stale_artifact_input_ref_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_static_probe_task(tmp_path, monkeypatch)
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)
        producer = workspace.run(
            "runtime_provider_tasks.generate",
            repo=repo,
            args={"output_path": "data.txt", "output_text": "ok\n"},
            placement="advisory",
            runtime={"provider": "static"},
        )
        ref = producer.output().artifact("data.txt").to_input()
        stale_ref = replace(ref, content_digest="sha256:" + ("0" * 64))

        with pytest.raises(WorkspaceControlError, match="digest mismatch"):
            workspace.run(
                "runtime_provider_tasks.generate",
                repo=repo,
                args={"source": stale_ref},
                placement="advisory",
                runtime={"provider": "static"},
            )
    finally:
        workspace.close()


def test_workspace_flow_fork_records_reopenable_trace_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_static_probe_task(tmp_path, monkeypatch)
    workspace_path = tmp_path / "ws"
    workspace = _make_workspace(workspace_path)
    reopened: ShepherdWorkspace | None = None
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)
        flow = workspace.flows.open(name="visual-variant-studio", metadata={"usecase": "uc1"})
        attempt = flow.fork(
            "runtime_provider_tasks.generate",
            repo=repo,
            name="contour-map",
            args={"output_path": "attempt.json", "output_content": {"variant": "contour-map"}},
            placement="advisory",
            runtime={"provider": "static"},
        )
        artifact_ref = attempt.output().artifact("attempt.json").to_input(label="contour-map")
        review = flow.fork(
            "runtime_provider_tasks.generate",
            repo=repo,
            name="review",
            after=[attempt],
            args={
                "candidate": artifact_ref,
                "output_path": "verdict.json",
                "output_content": {"selected": "contour-map"},
            },
            placement="advisory",
            runtime={"provider": "static"},
        )

        trace = flow.trace()
        assert {event["kind"] for event in trace["events"]} >= {
            "flow.opened",
            "flow.fork.requested",
            "provider.invocation",
            "run.lifecycle",
            "run.output.input",
            "run.output.published",
        }
        assert {(edge["kind"], edge["source"], edge["target"]) for edge in trace["edges"]} >= {
            ("causal_after", attempt.run_ref, review.run_ref),
            ("data_dependency", artifact_ref.output_id, review.run_ref),
        }
        provider_events = [event for event in trace["events"] if event["kind"] == "provider.invocation"]
        assert {event["provider_event_kind"] for event in provider_events} >= {
            "provider.invocation.started",
            "provider.invocation.completed",
        }
        assert {event["source"] for event in provider_events} == {"task_execution.metadata.provider_events"}
        assert {event["evidence_role"] for event in provider_events} == {"provider_provenance"}
        review.output().release()
        settled_trace = flow.trace()
        settlement = next(
            event
            for event in settled_trace["events"]
            if event["kind"] == "run.output.settled" and event["run_ref"] == review.run_ref
        )
        assert settlement["state"] == "released"
        assert isinstance(settlement["settlement_ref"], str)

        workspace.close()
        reopened = ShepherdWorkspace.discover(workspace_path)
        reopened_flow = reopened.flows.get(flow.flow_id)
        assert reopened_flow is not None
        assert [run.run_ref for run in reopened_flow.runs()] == [attempt.run_ref, review.run_ref]
        reopened_trace = reopened_flow.trace()
        assert reopened_trace["flow_id"] == flow.flow_id
        assert any(event["kind"] == "run.output.input" for event in reopened_trace["events"])
    finally:
        if reopened is not None:
            reopened.close()
        workspace.close()


def test_workspace_flow_fork_prelaunch_rejection_does_not_attach_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_static_probe_task(tmp_path, monkeypatch)
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)
        flow = workspace.flows.open(name="deferred-provider", metadata={"usecase": "guard"})

        with pytest.raises(RunStartError, match="deferred"):
            flow.fork(
                "runtime_provider_tasks.generate",
                repo=repo,
                name="codex-attempt",
                args={"output_path": "index.html", "output_text": "no launch"},
                placement="advisory",
                runtime={"provider": "codex"},
            )

        assert flow.runs() == ()
        assert workspace.runs.show("@latest") is None
        assert {event["kind"] for event in flow.trace()["events"]} == {"flow.opened"}
    finally:
        workspace.close()


def test_workspace_flow_fork_attaches_failed_started_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_static_probe_task(tmp_path, monkeypatch)
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)
        flow = workspace.flows.open(name="recovery", metadata={"usecase": "uc3"})

        with pytest.raises(RunStartError, match="static runtime artifact path"):
            flow.fork(
                "runtime_provider_tasks.generate",
                repo=repo,
                name="draft-v1",
                args={"output_path": "../escape.html", "output_text": "bad"},
                placement="advisory",
                runtime={"provider": "static"},
                metadata={"failed_run": "draft-v1"},
            )

        attached = flow.runs()
        assert len(attached) == 1
        assert attached[0].status == "failed"
        trace = flow.trace()
        assert {event["kind"] for event in trace["events"]} >= {
            "flow.fork.requested",
            "flow.failed_run",
            "run.lifecycle",
        }
        lifecycle = next(event for event in trace["events"] if event["kind"] == "run.lifecycle")
        assert lifecycle["status"] == "failed"
    finally:
        workspace.close()


def test_workspace_flow_trace_reconstructs_uc3_logical_retry_after_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_static_probe_task(tmp_path, monkeypatch)
    workspace_path = tmp_path / "ws"
    workspace = _make_workspace(workspace_path)
    reopened: ShepherdWorkspace | None = None
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)
        flow = workspace.flows.open(name="pipeline-recovery", metadata={"usecase": "uc3"})

        plan = flow.fork(
            "runtime_provider_tasks.generate",
            repo=repo,
            name="plan",
            args={"output_path": "plan.json", "output_content": {"step": "plan"}},
            placement="advisory",
            runtime={"provider": "static"},
            metadata={"logical_boundary": "plan"},
        )
        draft = flow.fork(
            "runtime_provider_tasks.generate",
            repo=repo,
            name="draft-v1",
            after=[plan],
            args={"output_path": "index.html", "output_text": "<main>wrong direction</main>"},
            placement="advisory",
            runtime={"provider": "static"},
            metadata={"failed_run": "draft-v1"},
        )
        inspector = flow.fork(
            "runtime_provider_tasks.generate",
            repo=repo,
            name="inspector",
            after=[draft],
            args={"output_path": "diagnosis.json", "output_content": {"reason": "wrong direction"}},
            placement="advisory",
            runtime={"provider": "static"},
        )
        plan_input = plan.output().artifact("plan.json").to_input(label="retry-plan")
        retry = flow.fork(
            "runtime_provider_tasks.generate",
            repo=repo,
            name="retry",
            after=[plan, inspector],
            args={
                "plan": plan_input,
                "output_path": "index.html",
                "output_text": "<main>corrected</main>",
            },
            placement="advisory",
            runtime={"provider": "static"},
            metadata={"retry_run": "retry-from-plan"},
        )

        workspace.close()
        reopened = ShepherdWorkspace.discover(workspace_path)
        reopened_flow = reopened.flows.get(flow.flow_id)
        assert reopened_flow is not None

        trace = reopened_flow.trace()
        event_kinds = {event["kind"] for event in trace["events"]}
        assert event_kinds >= {
            "flow.logical_boundary",
            "flow.failed_run",
            "flow.retry_run",
            "run.output.input",
        }
        assert {(edge["kind"], edge["source"], edge["target"]) for edge in trace["edges"]} >= {
            ("causal_after", plan.run_ref, draft.run_ref),
            ("causal_after", draft.run_ref, inspector.run_ref),
            ("causal_after", plan.run_ref, retry.run_ref),
            ("causal_after", inspector.run_ref, retry.run_ref),
            ("data_dependency", plan_input.output_id, retry.run_ref),
        }
    finally:
        if reopened is not None:
            reopened.close()
        workspace.close()


@pytest.mark.workspace_native_jail
def test_workspace_run_static_runtime_provider_uses_launch_confined_when_jail_resolves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_static_probe_task(tmp_path, monkeypatch)
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)

        run = workspace.run(
            "runtime_provider_tasks.generate",
            repo=repo,
            args={"output_path": "index.html", "output_text": "jailed static provider\n"},
            placement="jail",
            runtime={"provider": "static"},
        )

        assert run.output().read_file("index.html") == (b"jailed static provider\n", 0o100644)
        record = workspace.runs.show(run.run_ref)
        assert record is not None
        assert record.enforcement == "jail"
        assert record.execution_evidence.requested_placement == "jail"
        assert record.execution_evidence.resolved_placement == "jail"
        assert record.execution_evidence.enforcement_basis == "launch_confined_attempted"
        assert record.execution_evidence.execution_descriptor is not None
        assert record.execution_evidence.execution_descriptor["provider"] == "static"
        execution = record.task_executions[0]
        assert execution.executor_kind == "confined_process"
        assert execution.executor_policy == "provider_runtime"
        assert execution.metadata["launch_confined_attempted"] is True
        assert execution.metadata["runtime_provider"] == "static"
        policy = record.launch_context.settlement_policy
        assert policy is not None
        enforcement = policy["execution_enforcement"]
        assert enforcement["provider"] == "static"
        assert enforcement["established_monitor"] == enforcement["requested_monitor"]
    finally:
        workspace.close()


@pytest.mark.parametrize(
    ("runtime", "message"),
    [
        ({"provider": "codex"}, "deferred"),
        ({"provider": "claude-headless"}, "aliases for Claude are not public"),
        ({"provider": "openai"}, "unsupported runtime provider"),
        ({"model": "sonnet"}, "runtime.model requires runtime.provider"),
        ({"provider": "static", "tools": ["Write"]}, "reserved for future use"),
    ],
)
def test_workspace_run_runtime_provider_rejections_fail_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime: dict[str, Any],
    message: str,
) -> None:
    source = _write_static_probe_task(tmp_path, monkeypatch)
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)

        with pytest.raises(RunStartError, match=message):
            workspace.run("runtime_provider_tasks.generate", repo=repo, runtime=runtime)

        assert workspace.runs.show("@latest") is None
    finally:
        workspace.close()


def test_workspace_run_claude_runtime_rejects_advisory_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_static_probe_task(tmp_path, monkeypatch)
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)

        with pytest.raises(RunStartError, match="requires placement='auto' or placement='jail'"):
            workspace.run(
                "runtime_provider_tasks.generate",
                repo=repo,
                placement="advisory",
                runtime={"provider": "claude", "model": "sonnet"},
            )

        assert workspace.runs.show("@latest") is None
    finally:
        workspace.close()


def test_workspace_run_claude_runtime_auto_rejects_without_native_jail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shepherd_dialect.workspace_control.workspace as workspace_module

    monkeypatch.setattr(workspace_module, "native_jail_available", lambda: False)
    source = _write_static_probe_task(tmp_path, monkeypatch)
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)

        with pytest.raises(RunStartError, match="requires native jail support"):
            workspace.run(
                "runtime_provider_tasks.generate",
                repo=repo,
                placement="auto",
                runtime={"provider": "claude", "model": "sonnet"},
            )

        assert workspace.runs.show("@latest") is None
    finally:
        workspace.close()


@pytest.mark.workspace_native_jail
@pytest.mark.skipif(not native_jail_available(), reason="native jail backend is not available on this host")
def test_workspace_run_claude_runtime_fake_transport_publishes_retained_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeWorkspaceClaudeTransport:
        def __init__(self, invocation: object) -> None:
            self.invocation = invocation
            self.provider_id = invocation.provider_id
            self.model = invocation.model_name or "fake-claude"

        def execute(
            self,
            task_body: object,
            stack: object,
            context: object,
            args: object,
            *,
            execution: object,
            confinement: object,
        ) -> ExecutionProviderResult:
            del task_body, stack, context, args
            output_path = str(self.invocation.kwargs.get("output_path") or "index.html")
            if output_path.endswith(".json"):
                body = '{"selected":"candidate","candidates":[{"id":"candidate","verdict":"pass","issues":[]}]}'
            else:
                body = "<!doctype html><title>fake Claude retained output</title>"
            script = (
                "import pathlib\n"
                f"path = pathlib.Path({output_path!r})\n"
                "path.parent.mkdir(parents=True, exist_ok=True)\n"
                f"path.write_text({body!r}, encoding='utf-8')\n"
                "scratch = pathlib.Path('.claude-scratch')\n"
                "scratch.mkdir(exist_ok=True)\n"
                "scratch.joinpath('private.log').write_text('must not be retained', encoding='utf-8')\n"
                "pathlib.Path('.claude-sdk-scratch').write_text('must not be retained', encoding='utf-8')\n"
            )
            proc = execution.launch_confined([sys.executable, "-B", "-c", script], confinement)
            assert proc.returncode == 0
            invocation_id = f"{self.provider_id}:fake"
            started = ProviderEvent(
                kind="provider.invocation.started",
                provider_id=self.provider_id,
                invocation_id=invocation_id,
                sequence=0,
                event_id=f"{invocation_id}:started",
                model=self.model,
                payload={"transport": "fake"},
            )
            completed = ProviderEvent(
                kind="provider.invocation.completed",
                provider_id=self.provider_id,
                invocation_id=invocation_id,
                sequence=1,
                event_id=f"{invocation_id}:completed",
                model=self.model,
                payload={"artifact_path": output_path},
            )
            return ExecutionProviderResult(
                outcome={"status": "ok", "artifact_path": output_path},
                provider_events=(started, completed),
            )

    monkeypatch.setattr(
        runtime_provider_module,
        "_WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS",
        SimpleNamespace(claude=_FakeWorkspaceClaudeTransport),
    )
    source = _write_static_probe_task(tmp_path, monkeypatch)
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)
        flow = workspace.flows.open(name="claude-fake-positive")

        attempt = flow.fork(
            "runtime_provider_tasks.generate",
            repo=repo,
            name="attempt",
            args={"output_path": "index.html"},
            placement="jail",
            runtime={"provider": "claude", "model": "sonnet"},
        )
        assert attempt.output().read_text("index.html").startswith("<!doctype html>")
        _assert_claude_retained_run_record(workspace, attempt.run_ref, model="sonnet")
        _assert_private_provider_paths_not_retained(attempt)

        candidate_ref = attempt.output().artifact("index.html").to_input(label="candidate")
        reviewer = flow.fork(
            "runtime_provider_tasks.generate",
            repo=repo,
            name="reviewer",
            args={"candidate": candidate_ref, "output_path": "verdict.json"},
            after=[attempt],
            placement="jail",
            runtime={"provider": "claude", "model": "sonnet"},
        )
        assert reviewer.output().read_json("verdict.json")["selected"] == "candidate"
        _assert_claude_retained_run_record(workspace, reviewer.run_ref, model="sonnet")
        _assert_private_provider_paths_not_retained(reviewer)

        record = workspace.runs.show(reviewer.run_ref)
        assert record is not None
        assert record.args_ref is not None
        args_payload = get_run_args(workspace.mg, record.args_ref)
        assert args_payload is not None
        assert args_payload["input_refs"] == [candidate_ref.to_json()]

        trace = flow.trace()
        provider_events = [event for event in trace["events"] if event["kind"] == "provider.invocation"]
        assert {event["provider_id"] for event in provider_events} == {"claude"}
        assert {event["evidence_role"] for event in provider_events} == {"provider_provenance"}
        assert ("data_dependency", candidate_ref.output_id, reviewer.run_ref) in {
            (edge["kind"], edge["source"], edge["target"]) for edge in trace["edges"]
        }

        attempt.output().select()
        reviewer.output().release()
        assert attempt.output().refresh().state == "selected"
        assert reviewer.output().refresh().state == "released"
    finally:
        workspace.close()


def _assert_claude_retained_run_record(workspace: ShepherdWorkspace, run_ref: str, *, model: str) -> None:
    record = workspace.runs.show(run_ref)
    assert record is not None
    assert record.status == "retained"
    assert record.enforcement == "jail"
    assert record.execution_evidence.resolved_placement == "jail"
    assert record.execution_evidence.enforcement_basis == "launch_confined_attempted"
    assert record.execution_evidence.execution_descriptor is not None
    assert record.execution_evidence.execution_descriptor["provider"] == "claude"
    execution = record.task_executions[0]
    assert execution.executor_kind == "confined_process"
    assert execution.executor_policy == "provider_runtime"
    assert execution.metadata["runtime_provider"] == "claude"
    assert execution.metadata["runtime_model"] == model
    assert execution.metadata["launch_confined_attempted"] is True
    assert isinstance(execution.metadata["provider_prompt_digest"], str)
    assert execution.metadata["provider_private_dirs"] == [
        ".shepherd-inputs",
        ".claude-scratch",
        ".claude-sdk-scratch",
    ]
    assert [event["kind"] for event in execution.metadata["provider_events"]] == [
        "provider.invocation.started",
        "provider.invocation.completed",
    ]
    policy = record.launch_context.settlement_policy
    assert policy is not None
    assert policy["runtime"] == {
        "requested": {"provider": {"id": "claude"}, "model": {"name": model}},
        "resolved": {"provider": "claude", "model": model},
    }


def _assert_private_provider_paths_not_retained(run: object) -> None:
    private_prefixes = (".shepherd-inputs", ".claude-scratch", ".claude-sdk-scratch")
    retained_private_paths = [
        path
        for path in run.output().changed_paths
        for prefix in private_prefixes
        if path == prefix or path.startswith(f"{prefix}/")
    ]
    assert retained_private_paths == []
