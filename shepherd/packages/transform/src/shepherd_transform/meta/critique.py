"""CritiqueTask: Analyze task design and suggest improvements.

This meta-task examines a task's structure, field definitions, docstring,
and implementation to identify potential issues and suggest improvements.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field
from shepherd_runtime.task.authoring import Input, Output, task  # type: ignore[no-redef]
from shepherd_runtime.task.markers import TaskRef


@task
class CritiqueTask(BaseModel):  # type: ignore[operator]
    """Analyze a task's design and suggest improvements.

    Examines the task's structure, field definitions, docstring,
    and implementation to identify potential issues.

    Evaluation criteria:
    - clarity: Are field names and descriptions clear?
    - completeness: Are all necessary inputs/outputs defined?
    - error_handling: Does the task handle edge cases?
    - type_safety: Are types appropriately specific?
    - documentation: Is the docstring informative?

    Example:
        >>> result = await scope.execute(
        ...     CritiqueTask(
        ...         target=MyTask,
        ...         criteria=["clarity", "completeness"],
        ...     )
        ... )
        >>> print(result.critique)
        >>> for suggestion in result.suggestions:
        ...     print(f"- {suggestion}")
    """

    # Inputs
    target: Annotated[  # type: ignore[valid-type]
        Input(TaskRef),
        Field(description="The task class to critique"),
    ]
    criteria: Annotated[  # type: ignore[valid-type]
        Input(list[str]),
        Field(
            default=["clarity", "completeness", "error_handling"],
            description="Aspects to evaluate: clarity, completeness, error_handling, type_safety, documentation",
        ),
    ]

    # Outputs
    critique: Annotated[  # type: ignore[valid-type]
        Output(str),
        Field(description="Detailed critique of the task design"),
    ]
    suggestions: Annotated[  # type: ignore[valid-type]
        Output(list[str]),
        Field(description="Specific actionable improvement suggestions"),
    ]
    severity: Annotated[  # type: ignore[valid-type]
        Output(Literal["minor", "moderate", "major"]),
        Field(description="Overall severity of issues found"),
    ]


__all__ = ["CritiqueTask"]
