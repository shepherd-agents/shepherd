"""Regression checks for package-local workflow docs."""

from __future__ import annotations

import re
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
VCS_CORE_ROOT = PACKAGE_ROOT.parents[1]
WORKSPACE_ROOT = VCS_CORE_ROOT.parent
CONVERGENCE_ROOT = WORKSPACE_ROOT / "docs" / "engineering" / "convergence"
RELEASE_READINESS_DOC = WORKSPACE_ROOT / "260619-release-readiness.md"

ROOT_READER_DOC_PATHS = (
    VCS_CORE_ROOT / "README.md",
    VCS_CORE_ROOT / "CONTRIBUTING.md",
    PACKAGE_ROOT / "README.md",
    PACKAGE_ROOT / "ARCHITECTURE.md",
)

COMMAND_RUNTIME_CONTRACT_DOC_PATHS = (
    *tuple(sorted(CONVERGENCE_ROOT.glob("*.md"))),
    PACKAGE_ROOT / "README.md",
    VCS_CORE_ROOT / "design" / "reference" / "DESIGN-substrate-spi.md",
    VCS_CORE_ROOT / "design" / "guides" / "GUIDE-implementing-a-substrate.md",
)

COMMAND_RUNTIME_CONTRACT_TEXT_PATHS = (
    *COMMAND_RUNTIME_CONTRACT_DOC_PATHS,
    PACKAGE_ROOT / "src" / "vcs_core" / "runtime_api.py",
)

CURRENT_COMMAND_SURFACE_DOC_PATHS = (
    RELEASE_READINESS_DOC,
    CONVERGENCE_ROOT / "execution-boundary.md",
    CONVERGENCE_ROOT / "runtime-call-api.md",
    PACKAGE_ROOT / "README.md",
    VCS_CORE_ROOT / "CONTRIBUTING.md",
    VCS_CORE_ROOT / "design" / "reference" / "DESIGN-substrate-spi.md",
    VCS_CORE_ROOT / "design" / "guides" / "GUIDE-implementing-a-substrate.md",
)

RETIRED_RECORD_INVOCATION_PATTERN = re.compile(r"\b(?:mg|vcs-core)\s+record\b")

MECHANICAL_REPLACEMENT_ARTIFACTS = (
    "mgvcs-core",
    "experimentalvcs_core",
    "experimentalspi",
    "runtime_substratespi",
    "execute_recordedexec",
)

STALE_TYPECHECK_PATTERNS = (
    re.compile(r"\b(?:typecheck|mypy)\b[^\n.]{0,120}\b(?:non-green|not green|opportunistic)\b", re.IGNORECASE),
    re.compile(r"\b(?:non-green|not green|opportunistic)\b[^\n.]{0,120}\b(?:typecheck|mypy)\b", re.IGNORECASE),
)


def _read(path: Path) -> str:
    return path.read_text()


def _squash(text: str) -> str:
    return " ".join(text.split())


def _is_historical_or_archived_doc(path: Path) -> bool:
    relative_parts = path.relative_to(VCS_CORE_ROOT).parts
    return "history" in relative_parts or "archive" in relative_parts or "archived-proposals" in relative_parts


def _is_current_design_doc(path: Path) -> bool:
    if _is_historical_or_archived_doc(path):
        return False
    header = "\n".join(_read(path).splitlines()[:12])
    return "**State:** current" in header


def _current_docs() -> dict[Path, str]:
    docs = set(ROOT_READER_DOC_PATHS)
    docs.update(path for path in (VCS_CORE_ROOT / "design").rglob("*.md") if _is_current_design_doc(path))
    return {path: _read(path) for path in sorted(docs)}


def test_package_readme_keeps_the_package_local_testing_story_truthful() -> None:
    text = _read(PACKAGE_ROOT / "README.md")

    assert "## Testing" in text
    assert "make smoke" in text
    assert "make guide_check" in text
    assert "make test_unit" in text
    assert "broader non-container package target" in text
    assert "unit tests only" not in text
    assert "check_fast" not in text
    assert "check_full" not in text
    assert "make test_integration" not in text
    assert "make test_cli" not in text
    assert "## Installed Mode" in text
    assert "make test_installed" in text
    assert "prelaunch handoff/release candidates" in text
    assert "../../CONTRIBUTING.md" in text
    assert "commands from `packages/core`" in text
    assert "The `packages/core` package currently provides" in text
    assert "`vcs-core/packages/core`" not in text
    assert "`vcs-core/design/`" not in text
    assert "secondary verification path" not in text
    assert "GUIDE-store-first.md" in text
    assert "## Podman Shakeout" in text
    assert "make podman_up" in text
    assert "make podman_shell" in text
    assert "make podman_exec" in text
    assert "make podman_demo" in text
    assert "RUN_NAME" in text


def test_substrate_guide_teaches_the_public_authoring_surface() -> None:
    """The substrate-implementation guide must teach the stable public surface,
    not the private definition module, and must cover the out-of-tree and
    execution paths (decisions.md `spi-top-level-promotion` /
    `substrate-conformance-kit`).
    """
    guide = _read(VCS_CORE_ROOT / "design" / "guides" / "GUIDE-implementing-a-substrate.md")

    # Teaches the stable home and the conformance kit.
    assert "vcs_core.spi" in guide
    assert "assert_substrate_driver_conformant" in guide
    # Covers the out-of-tree (entry-point) and execution paths.
    assert "## Path C" in guide
    assert "## Path D" in guide
    assert "vcscore.substrate_plugins" in guide
    assert "ExecutionBoundDriver" in guide
    # Never instructs importing the SPI from its private definition module.
    assert "from vcs_core._substrate_driver import" not in guide


def test_live_command_runtime_docs_do_not_teach_retired_record_invocation() -> None:
    for path in COMMAND_RUNTIME_CONTRACT_DOC_PATHS:
        text = _read(path)
        match = RETIRED_RECORD_INVOCATION_PATTERN.search(text)
        assert match is None, f"{path} reintroduced retired record invocation guidance: {match.group(0)!r}"

    closure_doc = _read(WORKSPACE_ROOT / "260617-1900-contracts.md")
    assert "vcs-core record` path has been retired" in closure_doc


def test_current_convergence_docs_use_installed_cli_name_for_exec() -> None:
    for path in sorted(CONVERGENCE_ROOT.glob("*.md")):
        text = _read(path)
        assert "mg exec" not in text, f"{path} reintroduced shorthand `mg exec`; use `vcs-core exec`"


def test_live_command_runtime_docs_do_not_contain_mechanical_replacement_artifacts() -> None:
    for path in COMMAND_RUNTIME_CONTRACT_TEXT_PATHS:
        text = _read(path)
        for artifact in MECHANICAL_REPLACEMENT_ARTIFACTS:
            assert artifact not in text, f"{path} contains mechanical replacement artifact {artifact!r}"


def test_runtime_call_api_does_not_reopen_resolved_boundary_questions() -> None:
    text = _read(WORKSPACE_ROOT / "docs" / "engineering" / "convergence" / "runtime-call-api.md")

    stale_phrases = (
        "currently unhomed",
        "no owner yet",
        "nowhere to go",
        "runtime should hand the device its backend",
        "Module name + placement",
        "resolve when the module lands",
    )
    for phrase in stale_phrases:
        assert phrase not in text

    assert "`backend-handle-dissolves` decision removed that reach" in text


def test_live_command_runtime_docs_name_the_stable_spi_and_runtime_api_homes() -> None:
    runtime_call_api = _read(WORKSPACE_ROOT / "docs" / "engineering" / "convergence" / "runtime-call-api.md")
    execution_boundary = _read(WORKSPACE_ROOT / "docs" / "engineering" / "convergence" / "execution-boundary.md")
    package_readme = _read(PACKAGE_ROOT / "README.md")
    spi_reference = _read(VCS_CORE_ROOT / "design" / "reference" / "DESIGN-substrate-spi.md")
    runtime_api_source = _read(PACKAGE_ROOT / "src" / "vcs_core" / "runtime_api.py")

    assert "vcs_core.spi" in runtime_call_api
    assert "vcs_core.runtime_api" in runtime_call_api
    assert "from vcs_core.spi import (" in runtime_call_api
    assert "vcs-core's SPI (`vcs_core.spi`, `SPI_VERSION = 0`, frozen)" in execution_boundary
    assert "from vcs_core.spi import (" in runtime_api_source
    for text in (runtime_call_api, execution_boundary, spi_reference, runtime_api_source):
        assert "vcs_core.experimental" not in text

    assert "Supported substrate-author SPI: `vcs_core.spi`" in package_readme
    assert "Supported substrate conformance kit: `vcs_core.spi.testing`" in package_readme
    assert "Supported consumer/runtime call API: `vcs_core.runtime_api`" in package_readme

    squashed_spi_reference = _squash(spi_reference)
    assert "The SPI is the substrate-authoring contract under `vcs_core.spi`" in squashed_spi_reference
    assert "The SPI is the substrate-authoring contract under `vcs_core.experimental.spi`" not in squashed_spi_reference


def test_release_readiness_doc_pins_current_command_surface() -> None:
    text = _squash(_read(RELEASE_READINESS_DOC))

    required_phrases = (
        "Public substrate authoring lives at `vcs_core.spi`.",
        "Public runtime consumers use `vcs_core.runtime_api`",
        "Command-bearing bindings expose `DriverSchema`",
        "binding-scoped `CommandContract` objects",
        "explicit command value sources (`cli`, `typed-json`, or `native`)",
        "Projection is backend-specific.",
        "Framework-owned execution options live outside driver params",
        "Performed events are not command dispatch.",
        "Treat the `260614-*` plans as historical implementation ledgers",
        "Run sandbox-sensitive checks outside the managed sandbox",
    )
    for phrase in required_phrases:
        assert phrase in text


def test_current_command_surface_docs_do_not_teach_retired_compatibility_paths() -> None:
    # These name retired symbols as data. Build the sensitive tokens with `+`
    # so no intact retired identifier ever appears in this file's source (not
    # even in this comment): the retirement guard
    # (test_legacy_command_retirement.py) greps live Python for those tokens,
    # and this docs-contract test must not trip it. `+` survives `ruff format`
    # (unlike adjacent-literal concatenation, which the formatter rejoins into
    # the intact token). Do not collapse the `+` splits.
    retired_current_guidance = (
        "`_command_" + "coercion.py`",
        "`_schema_" + "validation.py`",
        "test_command_" + "coercion.py",
        "test_schema_" + "validation.py",
        "vcs_core.experimental",
        "experimental modules keep compatibility",
        "temporary `dict` alias",
        "dict alias",
        "raw-spec compatibility wrapper",
        "CommandProjection compatibility dataclass",
        "`VcsCore.substrates` is the legacy-lifecycle view",
        "`mg.substrates` is the legacy-lifecycle view",
    )
    for path in CURRENT_COMMAND_SURFACE_DOC_PATHS:
        text = _read(path)
        for phrase in retired_current_guidance:
            assert phrase not in text, f"{path} teaches retired command-surface guidance: {phrase!r}"


def test_primary_onboarding_docs_keep_store_first_as_default_path() -> None:
    package_readme = _read(PACKAGE_ROOT / "README.md")
    guides_index = _read(VCS_CORE_ROOT / "design" / "guides" / "README.md")
    store_first = _read(VCS_CORE_ROOT / "design" / "guides" / "GUIDE-store-first.md")
    supported_surfaces_link = "../../packages/core/README.md#supported-surfaces"

    assert "For most internal adopters, start with" in package_readme
    assert "GUIDE-store-first.md" in package_readme
    assert "most mature interactive workflow today is" not in package_readme
    assert "## Supported Surfaces" in package_readme
    assert "GUIDE-integration.md" in package_readme

    assert "Recommended first read:" in guides_index
    assert "Default onboarding path for normal internal adopters" in guides_index
    assert f"[Supported Surfaces]({supported_surfaces_link})" in guides_index
    assert "GUIDE-podman-shakeout.md" in guides_index
    assert "Podman-based Linux shakeout harness" in guides_index

    assert "## Who This Is For" in store_first
    assert "## What This Does Not Cover" in store_first
    assert "## Platform Expectations" in store_first
    assert "GUIDE-integration.md" in store_first
    assert f"[Supported Surfaces]({supported_surfaces_link})" in store_first


def test_contributing_keeps_the_authoritative_collaboration_loop_aligned() -> None:
    text = _read(VCS_CORE_ROOT / "CONTRIBUTING.md")

    assert "uv sync --all-groups" in text
    assert "workspace dependencies used by the package-local loop" in text
    assert "make -C packages/core <target>" in text
    assert "## Support Matrix" in text
    assert "`supported`" in text
    assert "`experimental`" in text
    assert "`sharp edge`" in text
    assert "## Static Checks" in text
    assert "## Quick Checks" in text
    assert "## Release / Handoff Bar" in text
    assert "## Change-Type Checklist" in text
    assert "make -C packages/core smoke" in text
    assert "make -C packages/core guide_check" in text
    assert "make -C packages/core test_unit" in text
    assert "make -C packages/core test_installed" in text
    assert "make -C packages/core test_container" in text
    assert "make -C packages/core lint" in text
    assert "make -C packages/core typecheck" in text
    assert "uv run --directory packages/core pytest tests/unit/test_docs_contract.py -q" in text
    assert "already-synced local test" in text
    assert "not a fresh-machine" in text
    assert "vcs_core.experimental" not in text
    assert "`make lint` and `make typecheck` are now normal collaborator gates" in text
    assert "mandatory handoff gate" in text
    assert "unit-only" not in text
    assert "check_fast" not in text
    assert "check_full" not in text
    assert "test_integration" not in text
    assert "test_cli" not in text
    assert "make help" not in text


def test_root_readme_points_to_the_authoritative_release_gate_story() -> None:
    text = _read(VCS_CORE_ROOT / "README.md")

    assert "## Working Agreement" in text
    assert "## Workspace Boundary" in text
    assert "Host environment state outside that workspace is pass-through" in text
    assert "CONTRIBUTING.md" in text
    assert "## Installed CLI Handoff Gate" in text
    assert "make test_installed" in text
    assert "handoff/release candidates" in text
    assert "public-boundary changes" in text
    assert "cd packages/core" in text
    assert "coordinate reviewers directly" in text
    assert "../.github/CODEOWNERS" not in text
    assert "optional verification" not in text
    assert "secondary verification" not in text


def test_contributing_mentions_standalone_review_routing() -> None:
    text = _read(VCS_CORE_ROOT / "CONTRIBUTING.md")

    assert "coordinate reviewers directly" in text
    assert "../.github/CODEOWNERS" not in text


def test_workspace_boundary_contract_is_documented_on_durable_surfaces() -> None:
    package_readme = _read(PACKAGE_ROOT / "README.md")
    model = _read(VCS_CORE_ROOT / "design" / "overview" / "MODEL.md")
    cli_tutorial = _read(VCS_CORE_ROOT / "design" / "guides" / "GUIDE-cli-tutorial.md")
    cli_porcelain = _read(VCS_CORE_ROOT / "design" / "reference" / "DESIGN-cli-porcelain.md")

    for text in (package_readme, model, cli_tutorial, cli_porcelain):
        assert "initialized workspace root" in text
        assert ".vcscore/" in text
        assert "host environment state outside" in text.lower()
        assert "untracked" in text

    assert "repo-local `.venv`" in package_readme
    assert "Reversibility guarantees apply to workspace state only" in model
    assert "workspace file changes under the overlay are sandboxed" in cli_tutorial
    assert "all file changes are sandboxed" not in cli_tutorial
    assert "Managed workspace: /workspace/project" in cli_tutorial
    assert "Environment: host state outside workspace is untracked" in cli_tutorial
    assert "`metadata_change`" in cli_tutorial
    assert "Materialize pending operations to substrate remotes" in cli_porcelain
    assert "real world" not in cli_porcelain


def test_capture_authority_docs_match_linked_reducer_contract() -> None:
    package_readme = _read(PACKAGE_ROOT / "README.md")
    model = _read(VCS_CORE_ROOT / "design" / "overview" / "MODEL.md")
    capture_authority = _read(VCS_CORE_ROOT / "design" / "reference" / "DESIGN-capture-authority.md")
    integration = _read(VCS_CORE_ROOT / "design" / "guides" / "GUIDE-integration.md")

    for text in (package_readme, model, capture_authority, integration):
        assert "CaptureEvent" in text
        assert "vcs_core.fs_capture_reduction" in text
        assert "command" in text.lower()

    assert "metadata_change" in package_readme
    assert "Overlay reconciliation remains residual fallback" in model
    assert "Duplicate `(pid, proc_seq)` events" in capture_authority
    assert "failed_command_origin" in capture_authority
    assert 'tool_name="Bash"' in integration
    assert "`caused_by`" in integration


def test_current_docs_do_not_reintroduce_stale_typecheck_status() -> None:
    for path, text in _current_docs().items():
        for pattern in STALE_TYPECHECK_PATTERNS:
            match = pattern.search(text)
            assert match is None, f"{path} contains stale typecheck status wording: {match.group(0)!r}"

    contributing = _read(VCS_CORE_ROOT / "CONTRIBUTING.md")
    assert "`make lint` and `make typecheck` are now normal collaborator gates" in contributing


def test_current_docs_do_not_reintroduce_cli_state_authority() -> None:
    for path, text in _current_docs().items():
        assert "cli_state" not in text, f"{path} reintroduced removed CLI state authority"


def test_active_prelaunch_slice_folder_only_contains_current_active_work() -> None:
    active_dir = VCS_CORE_ROOT / "design" / "roadmap" / "prelaunch-slices" / "active"
    active_readme = _read(active_dir / "README.md")
    bundle_readme = _read(VCS_CORE_ROOT / "design" / "roadmap" / "prelaunch-slices" / "README.md")
    active_plan_names = sorted(path.name for path in active_dir.glob("PLAN-*.md"))
    assert active_plan_names == [
        "PLAN-commons-projection-reliance.md",
        "PLAN-sequential-shepherd-orchestration-mvp.md",
    ]
    assert "current active slice" in active_readme.lower()
    for plan_name in active_plan_names:
        assert plan_name in active_readme
        assert f"active/{plan_name}" in bundle_readme
    sequential_plan = _read(active_dir / "PLAN-sequential-shepherd-orchestration-mvp.md")
    assert "vcs-core remains a sequential workspace substrate" in sequential_plan
    assert "serialize alternative attempts" in sequential_plan
    assert "one-live-child" in sequential_plan
    assert "No new vcs-core cohort store is required" in sequential_plan
    assert "parallel live child worlds under the same parent" in sequential_plan
    assert "marker substrate" in sequential_plan
    assert "Known Cleanup Gate" in sequential_plan
    assert "scope_registry_projection_mismatches()" in sequential_plan
    assert "live Store ref marked terminal or missing from the registry" in sequential_plan
    assert "invalid" in sequential_plan
    squashed_bundle = _squash(bundle_readme).lower()
    assert "prelaunch slice plan" in squashed_bundle
    assert "active" in squashed_bundle
    assert "future intake" in bundle_readme.lower()

    stale_active_phrases = (
        "temporarily holds several plans that are complete",
        "checked-in prelaunch slice docs show a transitional state",
        "retained here until archival",
        "complete in current tree; retained in `active/` pending archival move",
        "completed plans are being retained here pending archival moves",
    )
    for phrase in stale_active_phrases:
        assert phrase not in active_readme
        assert phrase not in bundle_readme


def test_current_rebase_docs_are_explicitly_deferred_and_unimplemented() -> None:
    rebase_docs = {
        path: text
        for path, text in _current_docs().items()
        if "`Store.rebase" in text
        or "`VcsCore.rebase" in text
        or "Store.rebase()" in text
        or "VcsCore.rebase()" in text
    }

    assert rebase_docs
    for path, text in rebase_docs.items():
        assert "NotImplementedError" in text, f"{path} mentions rebase without naming current runtime behavior"
        assert "deferred" in text.lower(), f"{path} mentions rebase without marking it deferred"


def test_current_world_vector_docs_track_substrate_driver_cutover() -> None:
    world_vectors = VCS_CORE_ROOT / "design" / "roadmap" / "substrate-framework" / "world-vectors"
    architecture = _read(world_vectors / "ARCHITECTURE-world-vectors.md")
    migration = _read(world_vectors / "PLAN-substrate-standardization-migration.md")
    standardization = _read(world_vectors / "DESIGN-substrate-standardization.md")
    spi = _read(VCS_CORE_ROOT / "design" / "reference" / "DESIGN-substrate-spi.md")
    squashed_migration = _squash(migration)
    squashed_spi = _squash(spi)

    assert "driver-ish substrate path" not in architecture
    assert "Phase C attachment point" not in architecture
    assert "SubstrateDriver` draft ingress lowered by the coordinator" in architecture
    assert "Provider-neutral `TaskTrace` checkpoint/append revisions with `frontier_id`" in architecture
    assert "Current status:" in migration
    assert "`vcscore.world_ref`, JSON `SessionState`, provider-neutral `TaskTrace`" in squashed_migration
    assert "Suggested driver order:" not in migration
    assert "Finish `vcscore.world_ref` as the first full command-driven" not in migration
    assert "Tree-backed v2 workspace state; substrate-first materialization" in migration
    assert "The production capture shadow-mode tranche is now landed" in migration
    assert "lazy production `WorldStorageManager` installation boundary" in squashed_migration
    assert "Failed selection leaves pending workspace-authority recovery" in migration
    assert "blocks later mutation and materialization" in migration
    # Tranche 3 materialization preference pinned in the migration plan.
    assert "Materialization reads from the substrate's `workspace/` Git tree" in migration
    assert "falls back to scalar `Store.read_workspace_file" in migration
    # Track A/B status post-Tranche-1-2-3 landing.
    assert "Track A: Tree-backed workspace revision wiring (landed)" in migration
    assert "Track B: Remaining scalar workspace contraction (ready to advance)" in migration
    assert "filesystem command scans, workspace baseline adoption, and overlay-merge reductions" in migration
    assert "post-publication journal failures close the original operation" in migration
    assert "orphan archive is idempotent across scalar/v2 partial cleanup" in migration
    assert "activation detects v2-only orphan scope authority" in migration
    assert "Production filesystem command scans, workspace baseline adoption, and overlay merge reductions" in migration
    assert "`operation_journal` and `workspace_authority` inventory domains" in migration
    assert "inventory selector evaluator" in migration
    assert "inventory-derived trace projection" in migration

    assert "The authority-ref rule is field-sensitive" in spi
    assert "`TransitionDraft.payload` is different: it is untrusted substrate payload data" in squashed_spi
    assert "control-plane or evidence-adjacent surfaces" in spi

    assert "EvidenceOnlyEnvelopeRecord" in standardization
    assert "coordinator-issued `EvidenceCitation` handles" in standardization
    assert "not shipped Phase 1 draft fields" not in standardization
    assert "EvidenceCitation` and `ReductionBatch` exist as private reducer-support records" in squashed_migration
