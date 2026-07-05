"""Semantic evidence for the Shepherd launch notebook helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from shepherd_dialect.workspace_control import (
    RUN_ARTIFACT_INPUT_SCHEMA,
    RunArtifactInputRef,
    ShepherdWorkspace,
)

REPO = Path(__file__).resolve().parents[1]
VISUAL_ARTIFACT_EXAMPLE = REPO / "examples" / "notebooks" / "visual_artifact"


@pytest.fixture(scope="module")
def launch_helpers():
    """Import the notebook helper using the same path shape as the notebooks."""
    if str(VISUAL_ARTIFACT_EXAMPLE) not in sys.path:
        sys.path.insert(0, str(VISUAL_ARTIFACT_EXAMPLE))
    from shepherd_usecases.visual_artifact import launch

    return launch


def test_uc1_review_helper_persists_multiple_artifact_refs_across_reopen(launch_helpers) -> None:
    """UC1 review dataflow is durable artifact refs, not only Python pre-read content."""
    prompt = launch_helpers.default_prompt()
    workspace = launch_helpers.open_workspace("test-uc1-review", prompt=prompt, metadata={"usecase": "uc1"})
    reopened: ShepherdWorkspace | None = None
    try:
        attempts = {
            variant: launch_helpers.run_static(
                workspace,
                name=variant,
                output_path=launch_helpers.ARTIFACT_PATH,
                output_text=launch_helpers.variant_html(prompt, variant),
                metadata={"variant": variant},
            )
            for variant in launch_helpers.variant_prompts()
        }
        candidate_refs = {
            f"candidate_{name}": launch_helpers.artifact_ref(run, label=name) for name, run in attempts.items()
        }
        reviewer = launch_helpers.run_with_artifact_refs(
            workspace,
            name="review",
            refs=candidate_refs,
            output_path=launch_helpers.VERDICT_PATH,
            output_content=launch_helpers.review_content(prompt, attempts),
            after=list(attempts.values()),
        )

        labels = _input_labels_for_run(launch_helpers, workspace, reviewer)
        assert labels == {"contour-map", "uphill-path"}
        assert _data_dependency_labels(workspace.flow.trace(), reviewer.run_ref) == labels

        root = workspace.root
        flow_id = workspace.flow.flow_id
        workspace.close()
        reopened = ShepherdWorkspace.discover(root)
        reopened_flow = reopened.flows.get(flow_id)
        assert reopened_flow is not None
        assert _data_dependency_labels(reopened_flow.trace(), reviewer.run_ref) == labels
    finally:
        if reopened is not None:
            reopened.close()
        workspace.close()


def test_launch_helper_bootstrap_validates_kernel_and_example_root(launch_helpers) -> None:
    """The public notebook setup check is executable, not README-only guidance."""
    launch_helpers.bootstrap(example_root=VISUAL_ARTIFACT_EXAMPLE)

    with pytest.raises(launch_helpers.NotebookSetupError, match="Cannot find the visual-artifact example package"):
        launch_helpers.bootstrap(example_root=VISUAL_ARTIFACT_EXAMPLE / "notebooks")


def test_static_task_declaration_is_provider_owned(launch_helpers) -> None:
    """The registered task entrypoint is a declaration for the static provider lane."""
    from shepherd_usecases.visual_artifact import tasks

    assert launch_helpers.TASK_REF == tasks.STATIC_ARTIFACT_TASK_REF
    with pytest.raises(RuntimeError, match="provider-owned"):
        tasks.static_artifact_task(object(), output_path="index.html")


def test_uc2_selector_helper_cites_every_evaluator_verdict(launch_helpers) -> None:
    """UC2 selector dataflow cites evaluator verdict artifacts for every model tier."""
    workspace = launch_helpers.open_workspace(
        "test-uc2-selector", prompt=launch_helpers.default_prompt(), metadata={"usecase": "uc2"}
    )
    try:
        runs = {
            config_name: launch_helpers.run_static(
                workspace,
                name=f"rightsize-{config_name}",
                output_path=launch_helpers.VERDICT_PATH,
                output_content=launch_helpers.evaluator_content(config_name, model),
                model=model,
                metadata={"model_tier": config_name, "model": model},
            )
            for config_name, model in launch_helpers.model_choices().items()
        }
        graded = launch_helpers.grade_runs(runs)
        verdict_refs = {
            f"verdict_{name}": launch_helpers.artifact_ref(item.run, launch_helpers.VERDICT_PATH, label=name)
            for name, item in graded.items()
        }
        selector = launch_helpers.run_with_artifact_refs(
            workspace,
            name="selector",
            refs=verdict_refs,
            output_path=launch_helpers.DECISION_PATH,
            output_content=launch_helpers.selector_content(graded),
            after=[item.run for item in graded.values()],
        )

        assert launch_helpers.read_json(selector, launch_helpers.DECISION_PATH)["kept"] == "mid"
        assert _input_labels_for_run(launch_helpers, workspace, selector) == {"high", "mid", "cheap"}
        assert _data_dependency_labels(workspace.flow.trace(), selector.run_ref) == {"high", "mid", "cheap"}
    finally:
        workspace.close()


def test_uc3_retry_helper_cites_plan_review_and_diagnosis_boundaries(launch_helpers) -> None:
    """UC3 retry is a logical/dataflow retry over artifact refs, not retained-output branching."""
    prompt = launch_helpers.default_prompt()
    brief, plan = launch_helpers.plan_for(prompt)
    workspace = launch_helpers.open_workspace("test-uc3-retry", prompt=prompt, metadata={"usecase": "uc3"})
    try:
        plan_run = launch_helpers.run_static(
            workspace,
            name="plan",
            output_path=launch_helpers.PLAN_PATH,
            output_content=plan,
            metadata={"logical_boundary": "plan"},
        )
        plan_ref = launch_helpers.artifact_ref(plan_run, launch_helpers.PLAN_PATH, label="retry-plan")
        draft_v1 = launch_helpers.run_with_artifact_ref(
            workspace,
            name="draft-v1",
            ref_name="plan",
            artifact_ref=plan_ref,
            output_path=launch_helpers.ARTIFACT_PATH,
            output_text=launch_helpers.draft_html(brief, corrupt=True),
            after=[plan_run],
            metadata={"failed_run": "draft-v1"},
        )
        draft_ref = launch_helpers.artifact_ref(draft_v1, label="failed-draft")
        reviewer = launch_helpers.run_with_artifact_ref(
            workspace,
            name="review",
            ref_name="candidate",
            artifact_ref=draft_ref,
            output_path=launch_helpers.VERDICT_PATH,
            output_content=launch_helpers.review_content(prompt, {"draft_v1": draft_v1}),
            after=[draft_v1],
        )
        selection = launch_helpers.selection_from_review(reviewer)
        issues = list(selection.candidates[0].get("issues", []))
        review_ref = launch_helpers.artifact_ref(reviewer, launch_helpers.VERDICT_PATH, label="draft-review")
        inspector = launch_helpers.run_with_artifact_ref(
            workspace,
            name="inspector",
            ref_name="review",
            artifact_ref=review_ref,
            output_path=launch_helpers.DIAGNOSIS_PATH,
            output_content=launch_helpers.diagnosis_content(issues),
            after=[reviewer],
        )
        diagnosis_ref = launch_helpers.artifact_ref(inspector, launch_helpers.DIAGNOSIS_PATH, label="retry-diagnosis")
        retry = launch_helpers.run_with_artifact_refs(
            workspace,
            name="retry",
            refs={"plan": plan_ref, "diagnosis": diagnosis_ref},
            output_path=launch_helpers.ARTIFACT_PATH,
            output_text=launch_helpers.draft_html(brief, corrupt=False),
            after=[plan_run, inspector],
            metadata={"retry_run": "retry-from-plan"},
        )

        trace = workspace.flow.trace()
        assert _input_labels_for_run(launch_helpers, workspace, draft_v1) == {"retry-plan"}
        assert _input_labels_for_run(launch_helpers, workspace, reviewer) == {"failed-draft"}
        assert _input_labels_for_run(launch_helpers, workspace, inspector) == {"draft-review"}
        assert _input_labels_for_run(launch_helpers, workspace, retry) == {"retry-plan", "retry-diagnosis"}
        assert _data_dependency_labels(trace, retry.run_ref) == {"retry-plan", "retry-diagnosis"}
        assert {"flow.logical_boundary", "flow.failed_run", "flow.retry_run"} <= {
            event["kind"] for event in trace["events"]
        }
        assert not hasattr(retry.output(), "branch")
    finally:
        workspace.close()


def test_launch_helper_rejects_reserved_artifact_ref_names(launch_helpers) -> None:
    """Notebook artifact citations cannot overwrite static-provider control fields."""
    workspace = launch_helpers.open_workspace("test-reserved-inputs", prompt=launch_helpers.default_prompt())
    try:
        run = launch_helpers.run_static(
            workspace,
            name="source",
            output_path="source.json",
            output_content={"ok": True},
        )
        ref = launch_helpers.artifact_ref(run, "source.json", label="source")

        for reserved in ("output_path", "output_text", "output_content", "artifact_path", "artifact_text", "runtime"):
            with pytest.raises(ValueError, match="artifact ref name is reserved"):
                launch_helpers.run_with_artifact_refs(
                    workspace,
                    name=f"reserved-{reserved}",
                    refs={reserved: ref},
                    output_path="reserved.json",
                    output_content={"should_not_launch": True},
                )

        with pytest.raises(ValueError, match="artifact refs must be a non-empty mapping"):
            launch_helpers.run_with_artifact_refs(
                workspace,
                name="empty-inputs",
                refs={},
                output_path="empty.json",
                output_content={"should_not_launch": True},
            )
    finally:
        workspace.close()


def test_launch_helper_accepts_canonical_artifact_ref_json(launch_helpers) -> None:
    """JSON-shaped artifact refs remain valid after helper validation canonicalizes them."""
    workspace = launch_helpers.open_workspace("test-json-input-ref", prompt=launch_helpers.default_prompt())
    try:
        source = launch_helpers.run_static(
            workspace,
            name="source",
            output_path="source.json",
            output_content={"ok": True},
        )
        ref_json = launch_helpers.artifact_ref(source, "source.json", label="source").to_json()
        consumer = launch_helpers.run_with_artifact_refs(
            workspace,
            name="consumer",
            refs={"source": ref_json},
            output_path="consumer.json",
            output_content={"used": True},
        )

        assert _input_labels_for_run(launch_helpers, workspace, consumer) == {"source"}
        assert _data_dependency_labels(workspace.flow.trace(), consumer.run_ref) == {"source"}
    finally:
        workspace.close()


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("not-a-ref", "must be an artifact reference"),
        ({"not": "an artifact ref"}, "must be an artifact reference"),
        ({"kind": RUN_ARTIFACT_INPUT_SCHEMA, "run_ref": ""}, "is malformed"),
    ],
)
def test_launch_helper_rejects_non_artifact_ref_values(launch_helpers, value: object, message: str) -> None:
    """The provenance-preserving helper must not silently accept non-ref values."""
    workspace = launch_helpers.open_workspace("test-invalid-input-values", prompt=launch_helpers.default_prompt())
    try:
        before = tuple(run.run_ref for run in workspace.flow.runs())
        with pytest.raises(ValueError, match=message):
            launch_helpers.run_with_artifact_refs(
                workspace,
                name="consumer",
                refs={"source": value},
                output_path="consumer.json",
                output_content={"should_not_launch": True},
            )
        assert tuple(run.run_ref for run in workspace.flow.runs()) == before
    finally:
        workspace.close()


def _guard_ref(*, label: str | None = None) -> RunArtifactInputRef:
    return RunArtifactInputRef(
        run_ref="run-source",
        output_id="output-source",
        path="source.json",
        label=label,
        content_digest="sha256:" + ("1" * 64),
    )


def test_launch_helper_rejects_skeleton_prefixed_artifact_ref_names(launch_helpers) -> None:
    """The reserved-name guard must fence skeleton_* refs (not every name)."""
    ref = _guard_ref()

    for reserved in ("output_path", "output_text", "output_content", "artifact_path", "artifact_text", "runtime"):
        with pytest.raises(ValueError, match="artifact ref name is reserved"):
            launch_helpers._validated_artifact_refs({reserved: ref})

    with pytest.raises(ValueError, match="artifact ref name is reserved"):
        launch_helpers._validated_artifact_refs({"skeleton_internal": ref})

    with pytest.raises(ValueError, match="artifact refs must be a non-empty mapping"):
        launch_helpers._validated_artifact_refs({})


def test_launch_helper_accepts_ordinary_artifact_ref_names(launch_helpers) -> None:
    """Regression pin for the ``startswith("")`` tautology: ordinary names pass.

    The tautology rejected every ref name as reserved; an ordinary ``source``
    ref must validate and canonicalize instead of being fenced.
    """
    validated = launch_helpers._validated_artifact_refs({"source": _guard_ref(label="source").to_json()})

    assert set(validated) == {"source"}
    ref = validated["source"]
    assert isinstance(ref, RunArtifactInputRef)
    assert ref.run_ref == "run-source"
    assert ref.path == "source.json"
    assert ref.label == "source"


def _input_labels_for_run(launch_helpers: object, workspace: object, run: object) -> set[str]:
    args_payload = launch_helpers.run_args(workspace, run)
    return {
        str(ref["label"])
        for ref in args_payload["input_refs"]
        if isinstance(ref, dict) and isinstance(ref.get("label"), str)
    }


def _data_dependency_labels(trace: dict[str, object], run_ref: str) -> set[str]:
    edges = trace["edges"]
    assert isinstance(edges, list)
    return {
        str(edge["label"])
        for edge in edges
        if isinstance(edge, dict)
        and edge.get("kind") == "data_dependency"
        and edge.get("target") == run_ref
        and isinstance(edge.get("label"), str)
    }
