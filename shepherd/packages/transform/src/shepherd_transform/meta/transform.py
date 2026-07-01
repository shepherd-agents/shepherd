"""TransformTask: Transform tasks based on natural language instructions.

This meta-task takes a task object and transformation instructions,
produces modified source code that can be reconstructed into a working task.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, Field
from shepherd_runtime.task.authoring import Input, Output, task  # type: ignore[no-redef]
from shepherd_runtime.task.markers import TaskRef

from shepherd_transform.source import extract_task_source, reconstruct_task

if TYPE_CHECKING:
    from shepherd_transform.grounding import EquivalenceLevel, GroundingResult


@dataclass(frozen=True)
class TransformProposal:
    """Reconstructed transform proposal from raw source.

    This stays on the transform owner path and accepts either class-form
    task classes or function-form callable tasks reconstructed from source.
    """

    target: object
    instruction: str
    source: str
    task: object
    explanation: str | None = None

    @property
    def task_class(self) -> type | None:
        """Return the reconstructed class for class-form proposals."""
        return self.task if isinstance(self.task, type) else None

    @property
    def is_class_form(self) -> bool:
        """Whether this proposal reconstructed to a class-form task."""
        return isinstance(self.task, type)

    @property
    def is_function_form(self) -> bool:
        """Whether this proposal reconstructed to a function-form callable task."""
        return not isinstance(self.task, type)

    def verify(
        self,
        original: object | None = None,
        test_cases: list[dict[str, Any]] | None = None,
        equivalence: EquivalenceLevel | None = None,
    ) -> GroundingResult:
        """Verify the proposal against its target or an explicit original task."""
        from shepherd_transform.grounding import (
            EquivalenceLevel,
            TaskInputSpec,
            TestInputGenerator,
            behavioral_grounding,
        )

        original_task = self.target if original is None else original

        if equivalence is None:
            equivalence = EquivalenceLevel.OUTCOME
        if test_cases is None:
            spec = TaskInputSpec.from_task(original_task)
            test_cases = TestInputGenerator(spec).generate_all()

        return behavioral_grounding(
            original_class=original_task,
            transformed_class=self.task,
            test_cases=test_cases,
            equivalence=equivalence,
        )


def build_transform_proposal(
    *,
    target: object,
    transformed_source: str,
    instruction: str,
    explanation: str | None = None,
    imports: list[str] | None = None,
    extra_namespace: dict[str, Any] | None = None,
    validate: bool = True,
) -> TransformProposal:
    """Reconstruct a transform proposal from source without class-only assumptions."""
    reconstructed = reconstruct_task(
        transformed_source,
        imports=imports,
        extra_namespace=extra_namespace,
        validate=validate,
    )
    return TransformProposal(
        target=target,
        instruction=instruction,
        source=transformed_source,
        task=reconstructed,
        explanation=explanation,
    )


@task
class TransformTask(BaseModel):  # type: ignore[operator]
    """Transform a task based on natural language instructions.

    Takes a task object and transformation instructions, produces
    modified source code that can be reconstructed into a working task.

    The transformed task is automatically reconstructed from the LLM's
    source code output using owner-path source validation and reconstruction.

    Example:
        >>> result = await scope.execute(
        ...     TransformTask(
        ...         target=Calculator,
        ...         instruction="Add logging for each operation",
        ...     )
        ... )
        >>>
        >>> # Verify the transformation preserves behavior
        >>> grounding = result.verify_transformation(Calculator)
        >>> if grounding.passed:
        ...     LoggingCalculator = result.transformed

    See Also:
        - verify_transformation(): Verify behavior is preserved
        - shepherd_transform.grounding: Behavioral grounding utilities
    """

    # Inputs
    target: Annotated[  # type: ignore[valid-type]
        Input(TaskRef),
        Field(description="The task object to transform"),
    ]
    instruction: Annotated[  # type: ignore[valid-type]
        Input(str),
        Field(description="Natural language description of the transformation"),
    ]
    preserve_behavior: Annotated[  # type: ignore[valid-type]
        Input(bool),
        Field(
            default=True,
            description="Whether to verify the transformation preserves original behavior",
        ),
    ]

    # Outputs
    transformed: Annotated[  # type: ignore[valid-type]
        Output(TaskRef),
        Field(description="The transformed task object (auto-reconstructed from source)"),
    ]
    transformed_source: Annotated[  # type: ignore[valid-type]
        Output(str),
        Field(description="Source code of the transformed task"),
    ]
    explanation: Annotated[  # type: ignore[valid-type]
        Output(str),
        Field(description="Explanation of changes made"),
    ]

    def verify_transformation(
        self,
        original_class: object,
        test_cases: list[dict[str, Any]] | None = None,
        equivalence: EquivalenceLevel | None = None,
    ) -> GroundingResult:
        """Verify the transformation preserves behavior.

        Uses behavioral grounding to compare the original and transformed
        task on a set of test cases. Test cases can be provided explicitly
        or auto-generated from the task's input specification.

        Args:
            original_class: The original task object (before transformation)
            test_cases: Optional test cases (auto-generated if None)
            equivalence: How strictly to compare outputs (default: OUTCOME)

        Returns:
            GroundingResult with verification details

        Raises:
            ValueError: If no transformed task is available

        Example:
            >>> result = await scope.execute(TransformTask(...))
            >>> grounding = result.verify_transformation(
            ...     original_class=MyTask,
            ...     equivalence=EquivalenceLevel.SEMANTIC,
            ... )
            >>> if grounding.passed:
            ...     print(f"Verified on {grounding.test_count} test cases")
        """
        from shepherd_transform.grounding import (
            EquivalenceLevel,
            TaskInputSpec,
            TestInputGenerator,
            behavioral_grounding,
        )

        if self.transformed is None:
            raise ValueError("No transformed task available - execute the task first")

        # Default equivalence level
        if equivalence is None:
            equivalence = EquivalenceLevel.OUTCOME

        # Auto-generate test cases if not provided
        if test_cases is None:
            spec = TaskInputSpec.from_task(original_class)
            generator = TestInputGenerator(spec)
            test_cases = generator.generate_all()

        return behavioral_grounding(
            original_class=original_class,
            transformed_class=self.transformed,
            test_cases=test_cases,
            equivalence=equivalence,
        )

    def build_proposal(
        self,
        *,
        imports: list[str] | None = None,
        extra_namespace: dict[str, Any] | None = None,
        validate: bool = True,
    ) -> TransformProposal:
        """Build a source-backed proposal for class-form or function-form transforms."""
        transformed_source = self.transformed_source
        if not transformed_source and self.transformed is not None:
            transformed_source = extract_task_source(self.transformed)
        if not transformed_source:
            raise ValueError("No transformed source available - execute the task first")

        return build_transform_proposal(
            target=self.target,
            transformed_source=transformed_source,
            instruction=self.instruction,
            explanation=self.explanation,
            imports=imports,
            extra_namespace=extra_namespace,
            validate=validate,
        )


__all__ = ["TransformProposal", "TransformTask", "build_transform_proposal"]
