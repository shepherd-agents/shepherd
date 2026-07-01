from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from vcs_core._command_admission import CommandAdmissionError
from vcs_core._patch_manager import PatchManager
from vcs_core._performed_event_admission import PerformedEventAdmissionError
from vcs_core._substrate_runtime import PerformedEventSpec, build_builtin_substrate_context
from vcs_core.recording import RecordingPipeline
from vcs_core.spi import (
    CapabilitySet,
    CommandRequest,
    CommandSpec,
    DriverContext,
    DriverIngressResult,
    DriverSchema,
    IngressRequest,
    ParamSpec,
    UnsupportedRequestError,
)
from vcs_core.store import Store
from vcs_core.substrates import FilesystemSubstrate
from vcs_core.types import EffectRecord, ScopeInfo
from vcs_core.vcscore import VcsCore


@dataclass
class AdmissionSubstrate:
    error: Exception | None = None

    name = "admission"
    binding = "admission"
    role = "admission"
    driver_id = "admission"
    driver_version = "test"
    commands = {
        "inspect": CommandSpec(description="Inspect", params={}),
        "limit": CommandSpec(
            description="Limit",
            params={"limit": ParamSpec(type="int", description="Synthetic limit.")},
        ),
    }

    @property
    def capabilities(self) -> CapabilitySet:
        return CapabilitySet(accepts=frozenset({CommandRequest}), selectable=False)

    def describe(self) -> DriverSchema:
        return DriverSchema(
            driver_id=self.driver_id,
            driver_version=self.driver_version,
            capabilities=self.capabilities,
            commands=self.commands,
        )

    def __post_init__(self) -> None:
        self.prepare_calls = 0
        self.seen_admission_params: list[dict[str, Any]] = []
        self.seen_prepare_params: list[dict[str, Any]] = []
        self.seen_admission_guard_depths: list[int] = []

    def bind_pipeline(self, pipeline, *, scope_queries=None) -> None:
        del pipeline, scope_queries

    def activate(self) -> None:
        pass

    def deactivate(self) -> None:
        pass

    def authority(self):
        return None

    def python_patches(self) -> tuple[object, ...]:
        return ()

    def prepare(self, context: DriverContext, request: IngressRequest) -> DriverIngressResult:
        del context
        if not isinstance(request, CommandRequest):
            raise UnsupportedRequestError(driver_id=self.driver_id, request_type=type(request))
        self.prepare_calls += 1
        self.seen_prepare_params.append(dict(request.params))
        return DriverIngressResult(
            effects=(EffectRecord(effect_type="Marker", metadata={"label": "recorded"}),),
            value={"ok": True},
        )

    def capture_adapters(self, context: DriverContext) -> tuple[object, ...]:
        del context
        return ()

    def validate_result(self, request: IngressRequest, result: DriverIngressResult) -> None:
        del request, result

    def validate_command_invocation(
        self,
        command: str,
        scope: ScopeInfo,
        *,
        params: Mapping[str, Any],
    ) -> None:
        del command, scope
        self.seen_admission_params.append(dict(params))
        self.seen_admission_guard_depths.append(getattr(PatchManager._tls, "depth", 0))
        if self.error is not None:
            raise self.error


@dataclass
class PerformedSubstrate:
    error: Exception | None = None

    name = "capture"

    def __post_init__(self) -> None:
        self.performed_calls = 0
        self.seen_performed_params: list[dict[str, Any]] = []
        self.seen_performed_guard_depths: list[int] = []

    def performed_event_specs(self) -> dict[str, PerformedEventSpec]:
        return {
            "inspect": PerformedEventSpec(
                params={"label": ParamSpec(type="str", required=False)},
                effect_types=("Marker",),
            )
        }

    def validate_performed_event(
        self,
        event: str,
        scope: ScopeInfo,
        *,
        params: Mapping[str, Any],
    ) -> None:
        del event, scope
        self.seen_performed_params.append(dict(params))
        self.seen_performed_guard_depths.append(getattr(PatchManager._tls, "depth", 0))
        if self.error is not None:
            raise self.error

    def performed_effects(
        self,
        event: str,
        scope: ScopeInfo,
        *,
        params: Mapping[str, Any],
    ) -> tuple[EffectRecord, ...]:
        del event, scope
        self.performed_calls += 1
        return (EffectRecord(effect_type="Marker", metadata={"label": params.get("label", "captured")}),)


def test_vcscore_exec_runs_admission_on_schema_coerced_params(workspace: Path) -> None:
    substrate = AdmissionSubstrate()
    mg = VcsCore(str(workspace), substrates=[substrate])  # type: ignore[list-item]
    mg.activate()
    try:
        task = mg.fork(mg.ground, "task-admission-coerce")

        outcome = mg.exec("admission", "limit", scope=task, limit="7")

        assert outcome.value == {"ok": True}
        assert substrate.seen_admission_params == [{"limit": 7}]
        assert substrate.seen_prepare_params == [{"limit": 7}]
    finally:
        mg.deactivate()


def test_vcscore_exec_wraps_plain_value_error_before_execute_or_record(workspace: Path) -> None:
    substrate = AdmissionSubstrate(error=ValueError("blocked by admission"))
    mg = VcsCore(str(workspace), substrates=[substrate])  # type: ignore[list-item]
    mg.activate()
    try:
        task = mg.fork(mg.ground, "task-admission-reject")

        with pytest.raises(CommandAdmissionError, match="blocked by admission"):
            mg.exec("admission", "inspect", scope=task)

        assert substrate.prepare_calls == 0
        assert mg.store.filter_effects(substrate="admission", ref=task.ref) == []
    finally:
        mg.deactivate()


def test_vcscore_exec_runs_admission_under_patch_manager_guard(workspace: Path) -> None:
    substrate = AdmissionSubstrate(error=ValueError("blocked by admission"))
    mg = VcsCore(str(workspace), substrates=[substrate])  # type: ignore[list-item]
    mg.activate()
    try:
        task = mg.fork(mg.ground, "task-admission-guard")

        with pytest.raises(CommandAdmissionError, match="blocked by admission"):
            mg.exec("admission", "inspect", scope=task)

        assert substrate.seen_admission_guard_depths == [1]
        assert substrate.prepare_calls == 0
        assert mg.store.filter_effects(substrate="admission", ref=task.ref) == []
    finally:
        mg.deactivate()


def test_vcscore_exec_propagates_unrelated_admission_bug(workspace: Path) -> None:
    substrate = AdmissionSubstrate(error=KeyError("missing-key"))
    mg = VcsCore(str(workspace), substrates=[substrate])  # type: ignore[list-item]
    mg.activate()
    try:
        task = mg.fork(mg.ground, "task-admission-bug")

        with pytest.raises(KeyError, match="missing-key"):
            mg.exec("admission", "inspect", scope=task)

        assert substrate.prepare_calls == 0
        assert mg.store.filter_effects(substrate="admission", ref=task.ref) == []
    finally:
        mg.deactivate()


def _patch_manager_for_scope(workspace: Path) -> tuple[Store, PatchManager, ScopeInfo]:
    store = Store(str(workspace / ".vcscore"))
    store.create_root_commit()
    pipeline = RecordingPipeline(store)
    manager = PatchManager(workspace, pipeline)
    scope = store.fork(Store.GROUND_REF, "task-patch-capture")
    pipeline.set_scope(scope)
    return store, manager, scope


def test_patch_manager_records_performed_event_after_admission(workspace: Path) -> None:
    substrate = PerformedSubstrate()
    store, manager, scope = _patch_manager_for_scope(workspace)

    oids = manager.record_performed_event(substrate, "inspect", {"label": "observed"}, scope=scope)

    assert len(oids) == 1
    assert substrate.performed_calls == 1
    assert substrate.seen_performed_params == [{"label": "observed"}]
    assert substrate.seen_performed_guard_depths == [1]
    effects = store.filter_effects(substrate="capture", ref=scope.ref)
    assert [effect.metadata["label"] for effect in effects] == ["observed"]


def test_patch_manager_runs_performed_event_admission_before_recording(workspace: Path) -> None:
    substrate = PerformedSubstrate(error=ValueError("blocked by performed admission"))
    store, manager, scope = _patch_manager_for_scope(workspace)

    with pytest.raises(PerformedEventAdmissionError, match="blocked by performed admission"):
        manager.record_performed_event(substrate, "inspect", {}, scope=scope)

    assert substrate.performed_calls == 0
    assert store.filter_effects(substrate="capture", ref=scope.ref) == []


def test_patch_manager_rejects_non_effect_performed_event_items(workspace: Path) -> None:
    @dataclass
    class InvalidPerformedSubstrate(PerformedSubstrate):
        def performed_effects(
            self,
            event: str,
            scope: ScopeInfo,
            *,
            params: Mapping[str, Any],
        ) -> tuple[object, ...]:
            del event, scope, params
            self.performed_calls += 1
            return ("not-an-effect",)

    substrate = InvalidPerformedSubstrate()
    store, manager, scope = _patch_manager_for_scope(workspace)

    with pytest.raises(TypeError, match="performed event 'inspect' returned non-EffectRecord item: str"):
        manager.record_performed_event(substrate, "inspect", {}, scope=scope)

    assert substrate.performed_calls == 1
    assert store.filter_effects(substrate="capture", ref=scope.ref) == []


def test_patch_manager_rejects_unknown_performed_event(workspace: Path) -> None:
    substrate = PerformedSubstrate()
    store, manager, scope = _patch_manager_for_scope(workspace)

    with pytest.raises(PerformedEventAdmissionError, match="has no performed event named 'missing'"):
        manager.record_performed_event(substrate, "missing", {}, scope=scope)

    assert substrate.performed_calls == 0
    assert store.filter_effects(substrate="capture", ref=scope.ref) == []


def test_patch_manager_rejects_unknown_performed_event_params(workspace: Path) -> None:
    substrate = PerformedSubstrate()
    store, manager, scope = _patch_manager_for_scope(workspace)

    with pytest.raises(PerformedEventAdmissionError, match="unknown parameter\\(s\\): extra"):
        manager.record_performed_event(substrate, "inspect", {"extra": True}, scope=scope)

    assert substrate.performed_calls == 0
    assert store.filter_effects(substrate="capture", ref=scope.ref) == []


def test_patch_manager_rejects_malformed_performed_event_contract_before_provider_code(workspace: Path) -> None:
    @dataclass
    class MalformedSpecSubstrate(PerformedSubstrate):
        def performed_event_specs(self) -> dict[str, PerformedEventSpec]:
            return {"inspect": PerformedEventSpec(effect_types="Marker")}  # type: ignore[arg-type]

    substrate = MalformedSpecSubstrate()
    store, manager, scope = _patch_manager_for_scope(workspace)

    with pytest.raises(PerformedEventAdmissionError, match="effect_types must be a tuple of strings"):
        manager.record_performed_event(substrate, "inspect", {}, scope=scope)

    assert substrate.performed_calls == 0
    assert store.filter_effects(substrate="capture", ref=scope.ref) == []


def test_patch_manager_passes_immutable_normalized_performed_params_to_validator(workspace: Path) -> None:
    @dataclass
    class MutatingValidatorSubstrate(PerformedSubstrate):
        def validate_performed_event(
            self,
            event: str,
            scope: ScopeInfo,
            *,
            params: Mapping[str, Any],
        ) -> None:
            del event, scope
            params["label"] = "mutated"  # type: ignore[index]

    substrate = MutatingValidatorSubstrate()
    store, manager, scope = _patch_manager_for_scope(workspace)

    with pytest.raises(TypeError):
        manager.record_performed_event(substrate, "inspect", {"label": "observed"}, scope=scope)

    assert substrate.performed_calls == 0
    assert store.filter_effects(substrate="capture", ref=scope.ref) == []


def test_patch_manager_rejects_undeclared_performed_effect_type(workspace: Path) -> None:
    @dataclass
    class WrongEffectTypeSubstrate(PerformedSubstrate):
        def performed_effects(
            self,
            event: str,
            scope: ScopeInfo,
            *,
            params: Mapping[str, Any],
        ) -> tuple[EffectRecord, ...]:
            del event, scope, params
            self.performed_calls += 1
            return (EffectRecord(effect_type="Other", metadata={"label": "bad"}),)

    substrate = WrongEffectTypeSubstrate()
    store, manager, scope = _patch_manager_for_scope(workspace)

    with pytest.raises(TypeError, match="undeclared effect type 'Other'; allowed: Marker"):
        manager.record_performed_event(substrate, "inspect", {}, scope=scope)

    assert substrate.performed_calls == 1
    assert store.filter_effects(substrate="capture", ref=scope.ref) == []


def test_filesystem_performed_write_rejects_non_bytes_content(workspace: Path) -> None:
    store, manager, scope = _patch_manager_for_scope(workspace)
    substrate = FilesystemSubstrate(build_builtin_substrate_context(store))

    with pytest.raises(PerformedEventAdmissionError, match="event 'write' parameter 'content' expected bytes"):
        manager.record_performed_event(substrate, "write", {"path": "bad.txt", "content": 123}, scope=scope)

    assert store.filter_effects(substrate="filesystem", ref=scope.ref) == []


def test_filesystem_performed_read_rejects_content_param(workspace: Path) -> None:
    store, manager, scope = _patch_manager_for_scope(workspace)
    substrate = FilesystemSubstrate(build_builtin_substrate_context(store))

    with pytest.raises(PerformedEventAdmissionError, match="event 'read' got unknown parameter\\(s\\): content"):
        manager.record_performed_event(substrate, "read", {"path": "bad.txt", "content": b"ignored"}, scope=scope)

    assert store.filter_effects(substrate="filesystem", ref=scope.ref) == []
