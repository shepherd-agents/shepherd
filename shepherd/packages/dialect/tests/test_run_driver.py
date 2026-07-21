"""The dialect run driver, dispatched through the real coordinator.

PD5 acceptance: an in-process ``run`` executes through the public verbs — the
coordinator's reversible wrap forks an isolated run scope, ``prepare_bound``
receives the per-run capability, the body runs pointed at the working path,
identity comes back in ``portable_core`` (the consumer reads it, never
composes it), and the negotiation rule holds under simulated skew.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from vcs_core._driver_schema_validation import validate_driver_schema, validate_projectable_command
from vcs_core.runtime_api import (
    CALL_API_VERSION,
    CommandExecutionOptions,
    Store,
    VcsCore,
    build_builtin_substrate_context,
)
from vcs_core.spi import ExecutionAuthorityRequired, NetMode
from vcs_core.substrates import FilesystemSubstrate, MarkerSubstrate

from shepherd_dialect import ShepherdRunDriver
from shepherd_dialect.provider_runtime import (
    PROVIDER_INVOCATION_COMPLETED,
    PROVIDER_INVOCATION_STARTED,
    ExecutionProviderResult,
    ProviderEvent,
)

if TYPE_CHECKING:
    from vcs_core.runtime_substrate import HandlerStack


def demo_task(stack: HandlerStack, *, marker: str = "hello") -> dict[str, Any]:
    del stack
    return {"marker": marker}


def path_aware_task(stack: HandlerStack, *, working_path: str) -> dict[str, Any]:
    """A body that asks for the run scope's working path by name."""
    del stack
    return {"saw_working_path": working_path}


def capability_aware_task(
    stack: HandlerStack,
    *,
    working_path: str,
    execution: Any,
    confinement: Any,
) -> dict[str, Any]:
    """A body that asks for execution authority and lowered confinement."""
    del stack
    return {
        "working_path": working_path,
        "execution_working_path": str(execution.working_path),
        "confinement_writable_roots": list(confinement.writable_roots),
        "network": confinement.network.mode.value,
    }


class EventProvider:
    provider_id = "event-provider"

    def execute(self, task_body, stack, context, args, *, execution=None, confinement=None) -> ExecutionProviderResult:
        del task_body, stack, context, args, execution, confinement
        invocation_id = "event-provider:test"
        events = (
            ProviderEvent(
                kind=PROVIDER_INVOCATION_STARTED,
                provider_id=self.provider_id,
                invocation_id=invocation_id,
                sequence=0,
                event_id=f"{invocation_id}:started",
                payload={"source": "test"},
            ),
            ProviderEvent(
                kind=PROVIDER_INVOCATION_COMPLETED,
                provider_id=self.provider_id,
                invocation_id=invocation_id,
                sequence=1,
                event_id=f"{invocation_id}:completed",
                payload={"source": "test"},
            ),
        )
        return ExecutionProviderResult(outcome={"result": {"marker": "events"}}, provider_events=events)


class CapturingConfinementProvider:
    provider_id = "capture-confinement"

    def __init__(self) -> None:
        self.confinement = None

    def execute(self, task_body, stack, context, args, *, execution=None, confinement=None) -> dict[str, Any]:
        del task_body, stack, context, args, execution
        self.confinement = confinement
        return {"result": {"marker": "captured"}}


DEMO_TASK_ID = f"{__name__}:demo_task"


@pytest.fixture
def mg(tmp_path: Path, overlay_backend) -> VcsCore:
    root = tmp_path / "ws"
    root.mkdir()
    store = Store(str(root / ".vcscore"))
    ctx = build_builtin_substrate_context(store, workspace=root, config={})
    backend = overlay_backend
    vcscore = VcsCore(
        str(root),
        substrates=[MarkerSubstrate(ctx), FilesystemSubstrate(ctx, backend=backend), ShepherdRunDriver()],
        store=store,
    )
    vcscore.activate()
    yield vcscore
    vcscore.deactivate()


def test_call_api_version_is_pinned() -> None:
    assert CALL_API_VERSION == "v0.1"


def test_version_surfaces_interrogable_from_one_import_home() -> None:
    """The seam's version identifiers, from one place (runtime-call-api.md §5's
    decision procedure): a consumer checks call_api; a driver checks the SPI;
    an execution-bound driver additionally checks the capability version."""
    from vcs_core.runtime_api import version_surfaces

    surfaces = version_surfaces()
    assert surfaces["call_api"] == CALL_API_VERSION
    assert surfaces["ingestion_spi"] == 0  # the frozen ingestion contract
    assert surfaces["execution_capability"]  # additive, separately versioned


def test_run_through_the_verbs_returns_identity_in_portable_core(mg: VcsCore, tmp_path: Path) -> None:
    """Also graduates dialect-jailed-run check A1: a reversible run's
    working_path is verified distinct from ground (Phase D acceptance)."""
    outcome = mg.execute_recorded(
        "runtime",
        "run",
        scope=mg.ground,
        task_id=DEMO_TASK_ID,
        args={"marker": "pd5"},
        may="ReadOnly",
    )
    payload = outcome.value.transitions[0].payload
    assert payload["schema"] == "shepherd/run/v0"
    # A1: the carrier gave the run its own working path, not ground's root.
    ground_root = (tmp_path / "ws").resolve()
    assert payload["device_projection"]["working_path"] != str(ground_root)
    core = payload["portable_core"]
    assert core["outcome"]["result"] == {"marker": "pd5"}
    assert core["may"] == {"declared": "ReadOnly", "resolved": "ReadOnly", "source": "declared"}
    # The consumer READS identity from the result — never composes it.
    assert core["run_scope"]["scope_name"].startswith("run-")
    assert core["run_scope"]["world_id"]
    assert core["operation_id"]
    projection = payload["device_projection"]
    assert projection["isolation"] == "isolated"
    assert projection["provider"] == "in-process"


def test_run_driver_projects_provider_events_to_observations(mg: VcsCore) -> None:
    outcome = mg.execute_recorded(
        "runtime",
        "run",
        scope=mg.ground,
        task_id=DEMO_TASK_ID,
        may="Permissive",
        provider=EventProvider(),
    )

    result = outcome.value
    transition = result.transitions[0]
    assert len(result.observations) == 2
    assert transition.observation_ids == tuple(observation.observation_id for observation in result.observations)
    assert [observation.stable_observation["kind"] for observation in result.observations] == [
        "provider.invocation.started",
        "provider.invocation.completed",
    ]
    assert transition.payload["portable_core"]["outcome"]["result"] == {"marker": "events"}


def test_run_driver_records_parsed_runtime_options(mg: VcsCore) -> None:
    outcome = mg.execute_recorded(
        "runtime",
        "run",
        scope=mg.ground,
        task_id=DEMO_TASK_ID,
        runtime={
            "trace": {"label": "launch", "tags": ["visual", "static"]},
            "provider": "static-mock",
            "model": "sonnet",
        },
    )

    core = outcome.value.transitions[0].payload["portable_core"]
    assert core["runtime"] == {
        "trace": {"label": "launch", "tags": ["visual", "static"]},
        "provider": {"id": "static-mock"},
        "model": {"name": "sonnet"},
    }


def test_run_driver_rejects_runtime_authority_shaped_fields(mg: VcsCore) -> None:
    with pytest.raises(ValueError, match=r"unknown runtime field\(s\): may"):
        mg.execute_recorded(
            "runtime",
            "run",
            scope=mg.ground,
            task_id=DEMO_TASK_ID,
            may="ReadOnly",
            runtime={"may": "Permissive"},
        )


def test_run_driver_lowers_confinement_from_recorded_may_resolution(mg: VcsCore) -> None:
    provider = CapturingConfinementProvider()

    outcome = mg.execute_recorded(
        "runtime",
        "run",
        scope=mg.ground,
        task_id=DEMO_TASK_ID,
        may="ReadOnly",
        provider=provider,
    )

    assert provider.confinement is not None
    assert provider.confinement.writable_roots == ()
    assert provider.confinement.network.mode is NetMode.DENY_ALL
    core = outcome.value.transitions[0].payload["portable_core"]
    assert core["may"] == {"declared": "ReadOnly", "resolved": "ReadOnly", "source": "declared"}


def test_run_driver_lowers_defaulted_may_to_permissive_confinement(mg: VcsCore) -> None:
    provider = CapturingConfinementProvider()

    outcome = mg.execute_recorded(
        "runtime",
        "run",
        scope=mg.ground,
        task_id=DEMO_TASK_ID,
        provider=provider,
    )

    payload = outcome.value.transitions[0].payload
    assert provider.confinement is not None
    assert provider.confinement.writable_roots == (payload["device_projection"]["working_path"],)
    assert provider.confinement.network.mode is NetMode.ALLOW_ALL
    assert payload["portable_core"]["may"] == {
        "declared": None,
        "resolved": "Permissive",
        "source": "defaulted",
    }


def test_run_driver_lowers_confinement_from_per_binding_grants(mg: VcsCore, tmp_path: Path) -> None:
    """LC-3d hookup (a): when per-binding grants are present, confinement lowers from THEM through
    the same install() seam — writable_roots is the union of the ReadWrite roots, the ReadOnly root
    excluded — not from the whole-workspace may= profile."""
    import os

    from shepherd_dialect.confinement import BindingRootGrant

    docs = tmp_path / "docs"
    backend = tmp_path / "backend"
    docs.mkdir()
    backend.mkdir()
    provider = CapturingConfinementProvider()

    outcome = mg.execute_recorded(
        "runtime",
        "run",
        scope=mg.ground,
        task_id=DEMO_TASK_ID,
        may="ReadOnly",  # a whole-run ReadOnly may= would deny all writes; the grants override it
        binding_grants=[
            BindingRootGrant(binding="docs", root=str(docs), writable=False),
            BindingRootGrant(binding="backend", root=str(backend), writable=True),
        ],
        provider=provider,
    )

    assert provider.confinement is not None
    assert provider.confinement.writable_roots == (os.path.realpath(str(backend)),)
    assert provider.confinement.network.mode is NetMode.DENY_ALL
    # may= provenance is still recorded on the multi-binding path (grants are the enforced surface).
    core = outcome.value.transitions[0].payload["portable_core"]
    assert core["may"] == {"declared": "ReadOnly", "resolved": "ReadOnly", "source": "declared"}


def test_run_driver_absent_binding_grants_uses_may_path_unchanged(mg: VcsCore) -> None:
    """LC-3d hookup (b): absent per-binding grants, confinement lowers from may= exactly as before."""
    provider = CapturingConfinementProvider()

    mg.execute_recorded(
        "runtime",
        "run",
        scope=mg.ground,
        task_id=DEMO_TASK_ID,
        may="ReadOnly",
        provider=provider,
    )

    assert provider.confinement is not None
    assert provider.confinement.writable_roots == ()  # byte-identical to the pre-LC-3d may= path


def test_run_driver_args_schema_is_opaque_object() -> None:
    spec = ShepherdRunDriver().describe().commands["run"].params["args"]
    assert spec.type == "object"


def test_run_driver_schema_marks_python_only_params_nonprojectable() -> None:
    schema = ShepherdRunDriver().describe()

    validate_driver_schema(schema)
    assert tuple(schema.commands) == ("run",)
    result = validate_projectable_command(schema, "run")

    assert result.projectable is True
    assert result.projectable_params == ("task_id", "args", "may", "runtime")
    assert result.required_one_of == (("task_body", "task_id"),)
    assert {param.param_name for param in result.hidden_params} == {
        "task_body",
        "binding_grants",
        "provider",
        "substrate_handlers",
        "supervisor_handlers",
    }


def test_body_is_pointed_at_the_run_scopes_working_path(mg: VcsCore) -> None:
    outcome = mg.execute_recorded("runtime", "run", scope=mg.ground, task_id=f"{__name__}:path_aware_task")
    payload = outcome.value.transitions[0].payload
    saw = payload["portable_core"]["outcome"]["result"]["saw_working_path"]
    assert saw == payload["device_projection"]["working_path"]


def test_body_can_request_execution_capability_and_lowered_confinement(mg: VcsCore) -> None:
    outcome = mg.execute_recorded(
        "runtime",
        "run",
        scope=mg.ground,
        task_id=f"{__name__}:capability_aware_task",
        may="ReadOnly",
    )

    payload = outcome.value.transitions[0].payload
    result = payload["portable_core"]["outcome"]["result"]
    assert result["working_path"] == payload["device_projection"]["working_path"]
    assert result["execution_working_path"] == payload["device_projection"]["working_path"]
    assert result["confinement_writable_roots"] == []
    assert result["network"] == "deny_all"


def test_loud_opt_out_runs_against_ground(mg: VcsCore) -> None:
    outcome = mg.execute_recorded(
        "runtime",
        "run",
        scope=mg.ground,
        task_id=DEMO_TASK_ID,
        execution_options=CommandExecutionOptions(non_reversible_run=True),
    )
    assert outcome.value.transitions[0].payload["device_projection"]["isolation"] == "ground"


def test_negotiation_rule_under_simulated_skew() -> None:
    from vcs_core.spi import verify_execution_negotiation

    verify_execution_negotiation(ShepherdRunDriver())


def test_run_driver_resolves_from_configured_plugin_binding(tmp_path: Path) -> None:
    from vcs_core.config import VcsCoreConfig
    from vcs_core.discovery import resolve_bindings

    root = tmp_path / "ws"
    root.mkdir()
    repo_path = root / ".vcscore"
    repo_path.mkdir()
    store = Store(str(repo_path))
    store.create_root_commit()

    config = VcsCoreConfig(bindings={"runtime": {"type": "shepherd.run_driver"}})
    bindings = resolve_bindings(config, root, store)

    runtime = next(binding for binding in bindings if binding.binding_name == "runtime")
    assert runtime.substrate_type == "shepherd.run_driver"
    assert isinstance(runtime.instance, ShepherdRunDriver)
    assert runtime.instance.driver_id == "shepherd.run_driver"
    assert isinstance(runtime.instance.driver_id, str)


def test_run_specifier_validation_and_unknown_params(mg: VcsCore) -> None:
    with pytest.raises(ValueError, match="requires exactly one of: task_body, task_id"):
        mg.execute_recorded("runtime", "run", scope=mg.ground)
    with pytest.raises(ValueError, match="accepts only one of: task_body, task_id"):
        mg.execute_recorded("runtime", "run", scope=mg.ground, task_body=demo_task, task_id=DEMO_TASK_ID)
    with pytest.raises(ValueError, match="unknown parameter"):
        mg.execute_recorded("runtime", "run", scope=mg.ground, task_id=DEMO_TASK_ID, tsak_id="typo")
    with pytest.raises(ValueError, match=r"unknown parameter.*envelope"):
        mg.execute_recorded(
            "runtime",
            "run",
            scope=mg.ground,
            envelope={"schema": "shepherd.task_run_envelope.v1", "task_id": DEMO_TASK_ID},
        )


def test_registry_commands_name_their_phase(mg: VcsCore) -> None:
    with pytest.raises(ValueError, match="Unknown runtime command"):
        mg.execute_recorded("runtime", "list", scope=mg.ground)


def test_no_private_vcs_core_coupling() -> None:
    """The dialect's import discipline, junior-checkable: no vcs_core._* imports."""
    import ast

    src_root = Path(__file__).parents[1] / "src" / "shepherd_dialect"
    offenders: list[str] = []
    for path in src_root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            offenders.extend(n for n in names if n.startswith("vcs_core._"))
    assert offenders == []


def test_run_driver_keeps_one_enforced_may_resolution_site() -> None:
    import ast

    path = Path(__file__).parents[1] / "src" / "shepherd_dialect" / "run_driver.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    resolve_may_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "resolve_may"
    ]

    assert len(resolve_may_calls) == 1
    assert 'dict(params.get("runtime") or {})' not in source
    assert "lower_may_to_confinement(" not in source


def test_unauthorized_execution_refused_when_prepared_plain() -> None:
    from vcs_core.spi import CommandRequest

    driver = ShepherdRunDriver()
    with pytest.raises(ExecutionAuthorityRequired, match="refusing to run real"):
        driver.prepare(None, CommandRequest(command="run", params={}))


def test_run_driver_passes_the_exported_conformance_kit() -> None:
    """The kit (vcs_core.spi.testing) works on a real out-of-tree execution
    driver: structural SPI conformance + the ExecutionBoundDriver opt-in + the
    fail-closed negotiation rule. This turns the module-bottom self-checks in
    run_driver.py into suite-visible coverage, and is the canonical example a
    Path-C author copies (decisions.md `substrate-conformance-kit`).
    """
    from vcs_core.spi.testing import assert_execution_driver_conformant

    assert_execution_driver_conformant(ShepherdRunDriver())


def test_cli_route_mg_exec_dispatches_the_run(mg: VcsCore) -> None:
    """The literal CLI seam (PD3b acceptance, pinned): `mg exec runtime run
    -p task_id=… -p may=…` delegates verbatim to `VcsCore.exec(binding,
    command, scope=…, **params)` (cli.py → _cli_command_effects.run_exec →
    App.execute → mg.exec), which is exactly this call. Binding a dialect
    driver into the CLI-constructed App still needs a driver-plugin config
    (signposted follow-up); the dispatch route itself is identical.
    """
    outcome = mg.exec("runtime", "run", scope=mg.ground, task_id=DEMO_TASK_ID, may="Permissive")
    payload = outcome.value.transitions[0].payload
    assert payload["schema"] == "shepherd/run/v0"
    assert payload["portable_core"]["may"] == {
        "declared": "Permissive",
        "resolved": "Permissive",
        "source": "declared",
    }


def test_literal_canonical_cli_runtime_run_and_operation_show(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate-1 row 6: generated canonical CLI launch + operation inspection."""
    from click.testing import CliRunner
    from vcs_core.cli import main

    root = tmp_path / "row6"
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.syspath_prepend(str(root))
    (root / "row6_task.py").write_text(
        "def run(stack, marker='ok'):\n    del stack\n    return {'marker': marker}\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    init = runner.invoke(main, ["init", "."])
    assert init.exit_code == 0, init.output
    (root / "vcscore.toml").write_text(
        '[bindings.filesystem]\ntype = "filesystem"\n\n[bindings.runtime]\ntype = "shepherd.run_driver"\n',
        encoding="utf-8",
    )

    run = runner.invoke(
        main,
        [
            "sub",
            "runtime",
            "run",
            "--task-id",
            "row6_task:run",
            "--args",
            '{"marker":"cli"}',
            "--may",
            "Permissive",
            "--json",
        ],
    )
    assert run.exit_code == 0, run.output
    payload = json.loads(run.output)
    core = payload["value"]["transitions"][0]["payload"]["portable_core"]
    assert core["outcome"] == {"provider": "in-process", "result": {"marker": "cli"}, "status": "ok"}
    operation_id = core["operation_id"]

    operation = runner.invoke(main, ["operation", "show", operation_id])
    assert operation.exit_code == 0, operation.output
    assert f"Operation:    {operation_id}" in operation.output
    assert "Kind:         shepherd.run_driver.run" in operation.output


def test_undeclared_may_runs_permissive_and_is_recorded_as_defaulted(mg: VcsCore) -> None:
    """The loud default (`may-default-is-permissive`, amended): a run with no
    declared may= still lowers to Permissive, but the payload records the
    provenance — the defaulted population is countable, never silent.
    """
    outcome = mg.execute_recorded(
        "runtime",
        "run",
        scope=mg.ground,
        task_id=DEMO_TASK_ID,
        args={"marker": "loud"},
    )
    core = outcome.value.transitions[0].payload["portable_core"]
    assert core["may"] == {"declared": None, "resolved": "Permissive", "source": "defaulted"}
