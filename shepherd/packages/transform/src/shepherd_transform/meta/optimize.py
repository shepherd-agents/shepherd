"""OptimizeFromEffects: Optimize tasks based on execution feedback.

This meta-task analyzes completed task executions (including their effect
streams, inputs, and outputs) to identify inefficiencies and suggest
optimizations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, Field
from shepherd_runtime.task.authoring import Input, Output, task  # type: ignore[no-redef]
from shepherd_runtime.task.markers import CompletedTask, TaskRef

if TYPE_CHECKING:
    from shepherd_transform.grounding import EquivalenceLevel, GroundingResult


@task(cacheable=False)  # type: ignore[operator]
class OptimizeFromEffects(BaseModel):
    """Optimize a task based on execution feedback.

    Analyzes completed task executions to identify inefficiencies
    and suggest optimizations. Each execution carries the task source,
    input values, output values, and the full effect stream.

    Example:
        >>> # Execute the task a few times
        >>> result1 = MyTask(query="test1")
        >>> result2 = MyTask(query="test2")
        >>>
        >>> # Run optimization
        >>> optimized = OptimizeFromEffects(
        ...     executions=[result1, result2],
        ...     feedback="Task is slow on large inputs",
        ... )
        >>>
        >>> # Verify and deploy
        >>> if optimized.verify_transformation(MyTask).passed:
        ...     deploy(optimized.optimized)

    Optimization goals:
    - efficiency: Reduce unnecessary operations
    - reliability: Handle edge cases and errors
    - clarity: Improve code readability
    - performance: Optimize for speed/memory
    """

    # Inputs
    executions: Annotated[  # type: ignore[valid-type]
        Input(list[CompletedTask]),
        Field(description="Completed task instances from past executions"),
    ]
    feedback: Annotated[  # type: ignore[valid-type]
        Input(str),
        Field(
            default="",
            description="Optional user feedback about issues observed",
        ),
    ]
    optimization_goals: Annotated[  # type: ignore[valid-type]
        Input(list[str]),
        Field(
            default=["efficiency", "reliability"],
            description="What to optimize for: efficiency, reliability, clarity, performance",
        ),
    ]

    # Outputs
    optimized: Annotated[  # type: ignore[valid-type]
        Output(TaskRef),
        Field(description="The optimized task class (auto-reconstructed from source)"),
    ]
    optimized_source: Annotated[  # type: ignore[valid-type]
        Output(str),
        Field(description="Source code of the optimized task"),
    ]
    changes_made: Annotated[  # type: ignore[valid-type]
        Output(list[str]),
        Field(description="List of optimizations applied"),
    ]
    expected_improvement: Annotated[  # type: ignore[valid-type]
        Output(str),
        Field(description="Expected impact of optimizations"),
    ]

    def verify_transformation(
        self,
        original_class: type,
        test_cases: list[dict[str, Any]] | None = None,
        equivalence: EquivalenceLevel | None = None,
    ) -> GroundingResult:
        """Verify the optimization preserves behavior.

        Uses behavioral grounding to compare the original and optimized
        task on a set of test cases.

        Args:
            original_class: The original task class (before optimization)
            test_cases: Optional test cases (auto-generated if None)
            equivalence: How strictly to compare outputs (default: OUTCOME)

        Returns:
            GroundingResult with verification details

        Raises:
            ValueError: If no optimized task is available
        """
        from shepherd_transform.grounding import (
            EquivalenceLevel,
            TaskInputSpec,
            TestInputGenerator,
            behavioral_grounding,
        )

        if self.optimized is None:
            raise ValueError("No optimized task available - execute the task first")

        # Default equivalence level
        if equivalence is None:
            equivalence = EquivalenceLevel.OUTCOME

        # Auto-generate test cases if not provided
        if test_cases is None:
            spec = TaskInputSpec.from_task_class(original_class)
            generator = TestInputGenerator(spec)
            test_cases = generator.generate_all()

        return behavioral_grounding(
            original_class=original_class,
            transformed_class=self.optimized,
            test_cases=test_cases,
            equivalence=equivalence,
        )


__all__ = ["OptimizeFromEffects"]
