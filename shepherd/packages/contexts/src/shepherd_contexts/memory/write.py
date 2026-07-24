"""Write path: derive memory-worthy observations from a run's effect trace.

Pure and side-effect-free — call this *out-of-band*, after a run has settled
(select/release/discard) or after a TaskFailed. Feed the resulting
:class:`MemoryObservation` objects to ``backend.save(...)``.

The settlement *decision* (was a completed run selected or discarded?) is not in
the effect stream — it is the human's action at the review gate. Pass it in via
``disposition`` so the observation records the supervisor's judgment, which the
council identified as the highest-signal memory input.

Failures (TaskFailed) are always extracted: they are the canonical bugfix/root-
cause memory and need no human decision to be worth remembering.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from shepherd_contexts.memory.types import MemoryObservation

if TYPE_CHECKING:
    from collections.abc import Iterable

    from shepherd_core.effects import Effect

Disposition = Literal["select", "discard", "release"]


def observations_from_effects(
    effects: Iterable[Effect],
    *,
    project: str | None = None,
    disposition: Disposition | None = None,
    source: str | None = None,
) -> list[MemoryObservation]:
    """Extract memory-worthy observations from a run's effects.

    Args:
        effects: The run's effect sequence (e.g. a scope's stream).
        project: Project namespace for the observations.
        disposition: The human's settlement decision for a *completed* run
            (``select``/``release``/``discard``). Combined with TaskCompleted to
            emit a decision/anti-pattern observation. Failures ignore this.
        source: Provenance — the run/trace id these observations came from.

    Returns:
        Observations to persist via ``backend.save(...)``. Empty if nothing
        memory-worthy was found.
    """
    out: list[MemoryObservation] = []
    completed_tasks: list[str] = []

    for effect in effects:
        etype = getattr(effect, "effect_type", "")
        if etype == "task_failed":
            out.append(_observation_from_failure(effect, project=project, source=source))
        elif etype == "task_completed":
            # disposition applies per completed task (parallel to failures, so no
            # completion is silently dropped). A nameless completion falls back to
            # 'task' so it still yields an observation. Downstream topic_key dedupes.
            completed_tasks.append(getattr(effect, "task_name", None) or "task")

    # A completed run that the supervisor discarded is a high-signal anti-pattern;
    # one that was selected is a validated decision. One observation per completed
    # task (the human verdict applies to the run; topic_key dedupes repeats).
    if disposition:
        for task in completed_tasks:
            out.append(
                _observation_from_disposition(
                    task,
                    disposition=disposition,
                    project=project,
                    source=source,
                )
            )

    return out


def _observation_from_failure(
    effect: Effect,
    *,
    project: str | None,
    source: str | None,
) -> MemoryObservation:
    error = getattr(effect, "error", "") or "(no error message)"
    error_type = getattr(effect, "error_type", "") or "error"
    phase = getattr(effect, "phase", "") or ""
    last_tool = getattr(effect, "last_tool_name", None)
    loc = getattr(effect, "error_location", None)
    suggestions = getattr(effect, "suggestions", ()) or ()
    task = getattr(effect, "task_name", None) or "task"

    lines = [error]
    if phase:
        lines.append(f"Failed in phase: {phase}")
    if last_tool:
        lines.append(f"Last tool: {last_tool}")
    if loc:
        lines.append(f"At: {loc}")
    if suggestions:
        lines.append("Suggestions: " + "; ".join(suggestions))

    return MemoryObservation(
        type="bugfix",
        title=f"{task} failed: {error_type}",
        content="\n".join(lines),
        project=project,
        topic_key=f"failure:{error_type}",
        source=source or task,
    )


def _observation_from_disposition(
    task: str,
    *,
    disposition: Disposition,
    project: str | None,
    source: str | None,
) -> MemoryObservation:
    if disposition == "discard":
        return MemoryObservation(
            type="pattern",
            title=f"{task}: supervisor discarded the output",
            content=(
                f"A completed run of {task} was discarded at the review gate. "
                "Treat the approach as suspect for similar future tasks."
            ),
            project=project,
            topic_key=f"discarded:{task}",
            source=source or task,
        )
    # select / release -> a validated decision worth recalling positively.
    return MemoryObservation(
        type="decision",
        title=f"{task}: supervisor {disposition}ed the output",
        content=f"A completed run of {task} was {disposition}ed at the review gate.",
        project=project,
        topic_key=f"{disposition}:{task}",
        source=source or task,
    )


__all__ = ["Disposition", "observations_from_effects"]
