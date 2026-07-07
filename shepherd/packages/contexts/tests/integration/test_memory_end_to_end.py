"""End-to-end demo: MemoryContext recall -> task run -> logged MemoryRecalled effect.

Exercises the full council wedge with the deterministic MockProvider (no LLM,
no network): a task binds a MemoryContext, the recalled hints reach the
provider binding's system prompt, and the run's effect trace contains a
MemoryRecalled recording exactly what was surfaced. This is the "read path"
proving the advisory layer is both *visible to the agent* and *auditable in
the trace*.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel
from shepherd_contexts.memory import (
    InMemoryBackend,
    MemoryContext,
    MemoryHint,
    MemoryRecalled,
)
from shepherd_runtime.scope import Scope
from shepherd_runtime.task.authoring import Context, Output, task
from shepherd_tests import MockProvider

if TYPE_CHECKING:
    from collections.abc import Sequence

    from shepherd_core.effects import Effect


@task
class AnswerFromMemory(BaseModel):
    """Answer a question, optionally informed by recalled advisory memory."""

    memory: Context(MemoryContext)
    answer: Output(str)


async def test_memory_wedge_recall_runs_and_logs() -> None:
    backend = InMemoryBackend(
        [
            MemoryHint(
                title="claude auth",
                content="use CLAUDE_CODE_OAUTH_TOKEN for jailed runs",
                digest="d1",
                type="decision",
            )
        ]
    )
    memory = MemoryContext.create(backend, query="claude auth", project="shepherd")
    provider = MockProvider(name="demo", mock_responses=[{"text": "use the oauth token"}])

    async with Scope(root=True) as scope:
        scope.bind("memory", memory)
        scope.register_provider("default", provider, default=True)
        await AnswerFromMemory.arun(scope=scope)
        effects: Sequence[Effect] = [layer.effect for layer in scope.effects]

    # 1) The recall is logged as a MemoryRecalled effect in the trace.
    recalled = [e for e in effects if e.effect_type == "memory_recalled"]
    assert len(recalled) == 1
    assert isinstance(recalled[0], MemoryRecalled)
    assert recalled[0].backend == "memory"
    assert recalled[0].project == "shepherd"
    assert recalled[0].hint_titles == ("claude auth",)
    assert recalled[0].hint_digests == ("d1",)

    # 2) The recalled hint reached the provider binding the agent was run under
    #    (MemoryContext.configure() surfaced it via system_prompt_additions).
    assert provider.calls, "MockProvider should have been invoked"
    additions = provider.calls[0]["binding"].system_prompt_additions
    assert any("CLAUDE_CODE_OAUTH_TOKEN" in a for a in additions)

    # 3) The task completed (baseline: memory didn't break the run).
    assert [e for e in effects if e.effect_type == "task_completed"]
    # 4) Advisory-only: nothing mutated memory state. Effects attributed to the
    #    memory binding are only read-only / lifecycle records — no state
    #    mutation (no key_set / file_patch / similar) ever touches memory.
    _benign = {
        "memory_recalled",
        "context_prepared",
        "context_captured",
        "context_cleaned_up",
        "context_configured",
    }
    memory_binding_effects = [
        e for e in effects if getattr(e, "binding_name", None) == "memory"
    ]
    assert all(e.effect_type in _benign for e in memory_binding_effects)
