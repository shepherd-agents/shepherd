"""PlanDesignRefinement — extract principles, draft spike plan, produce refinement plan."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from shepherd_contexts import WorkspaceRef  # noqa: TC002
from shepherd_runtime.task.authoring import Context, Input, Output, task

from shepherd_authoring.checks import check_file_exists
from shepherd_authoring.tasks.draft_spike_plan import DraftSpikePlan
from shepherd_authoring.tasks.extract_principles import ExtractPrinciples


def _make_output_dir(design_document_path: str) -> Path:
    """Create .refinement/{doc-stem}/ output directory next to the design doc."""
    design_path = Path(design_document_path)
    stem = design_path.stem
    stem = stem.removeprefix("DESIGN-")
    output_dir = design_path.parent / ".refinement" / stem
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _extract_source_paths(design_text: str, workspace_path: str) -> list[str]:
    """Extract source file paths referenced in the design document.

    Looks for paths matching common package source patterns (e.g.,
    packages/*/src/**/*.py) in backtick-quoted strings and plain text.
    Returns only paths that actually exist on disk.
    """
    # Match paths like packages/foo/src/foo/bar.py or src/foo/bar.py
    pattern = r"(?:packages/[^\s`]+\.py|src/[^\s`]+\.py)"
    candidates = re.findall(pattern, design_text)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for path in candidates:
        # Strip trailing punctuation that might have been captured
        path = path.rstrip(".,;:)")
        if path not in seen:
            seen.add(path)
            unique.append(path)

    # Resolve against workspace and filter to existing files
    ws = Path(workspace_path)
    existing = []
    for rel_path in unique:
        full = ws / rel_path
        if full.exists():
            existing.append(str(full))

    return existing


@task
class PlanDesignRefinement(BaseModel):
    """Extract principles, draft spike plan, produce refinement plan.

    All outputs are written under .refinement/{doc-name}/ next to the
    source design document. The source document is copied into a
    documents/ subdirectory — the original is never modified.
    """

    # Inputs
    design_document_path: Input(str) = Field(description="Path to design doc")
    max_iterations: Input(int) = Field(default=5)
    target_score: Input(float) = Field(default=8.0)

    # Context
    workspace: Context[WorkspaceRef]

    # Outputs
    plan_path: Output(str) = Field(default="")
    principles_path: Output(str) = Field(default="")
    spike_plan_path: Output(str) = Field(default="")
    output_dir: Output(str) = Field(default="")

    def execute(self) -> None:
        # Precondition: design doc must exist
        design_path = Path(self.design_document_path)
        if not check_file_exists(design_path):
            raise FileNotFoundError(f"Design document not found: {self.design_document_path}")

        # Set up output directory
        out_dir = _make_output_dir(self.design_document_path)
        docs_dir = out_dir / "documents"
        docs_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir = str(out_dir)

        # Copy source design doc into documents/ (original is never modified)
        working_design = docs_dir / "design.md"
        shutil.copy2(design_path, working_design)

        # Extract source file paths from the design document for spike context
        design_text = design_path.read_text()
        workspace_path = str(Path(self.design_document_path).resolve().parent.parent)
        # Use the workspace ref's path if available, fall back to inferred
        if self.workspace and hasattr(self.workspace, "path"):
            workspace_path = str(self.workspace.path)
        source_paths = _extract_source_paths(design_text, workspace_path)

        # Extract principles (LLM reads the original, writes to output dir)
        extract = self.run_stage_sync(
            "extract_principles",
            ExtractPrinciples,
            design_document_path=self.design_document_path,
            output_dir=str(out_dir),
        )
        principles = extract.principles or []
        principles_path = extract.principles_path or ""
        self.principles_path = principles_path

        # Postcondition: principles were extracted and file was written
        if not principles:
            raise ValueError(
                "ExtractPrinciples returned no principles. Check that the design document has extractable content."
            )
        if principles_path and not check_file_exists(Path(principles_path)):
            raise FileNotFoundError(
                f"ExtractPrinciples reported principles_path={principles_path!r} but the file was not written to disk."
            )

        # Draft spike plan (LLM reads design + principles + source files)
        draft = self.run_stage_sync(
            "draft_spike_plan",
            DraftSpikePlan,
            design_document_path=self.design_document_path,
            principles=principles,
            output_dir=str(out_dir),
            source_paths=source_paths,
        )
        spike_plan_path = draft.spike_plan_path or ""
        self.spike_plan_path = spike_plan_path

        # Copy spike plan into documents/ for the refiner to work on
        if spike_plan_path and Path(spike_plan_path).exists():
            shutil.copy2(spike_plan_path, docs_dir / "spikes.md")
        elif spike_plan_path:
            raise FileNotFoundError(
                f"DraftSpikePlan reported spike_plan_path={spike_plan_path!r} but the file was not written to disk."
            )

        # Write REFINEMENT-PLAN.yaml into the output dir.
        # document_paths point to the working copies in documents/.
        plan = {
            "design_document_path": self.design_document_path,
            "output_dir": str(out_dir),
            "principles_path": principles_path,
            "principles": principles,
            "spike_plan_path": spike_plan_path,
            "source_paths": source_paths,
            "max_iterations": self.max_iterations,
            "target_score": self.target_score,
            "document_paths": {
                "design": str(working_design),
                "spikes": str(docs_dir / "spikes.md"),
            },
        }
        plan_path = out_dir / "REFINEMENT-PLAN.yaml"
        plan_path.write_text(yaml.dump(plan, default_flow_style=False))
        self.plan_path = str(plan_path)


__all__ = ["PlanDesignRefinement"]
