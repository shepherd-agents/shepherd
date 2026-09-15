"""Tests for the advisory memory context.

Covers the council's consensus wedge:
- recall surfaces hints into the system prompt (read path)
- the recall is logged as a MemoryRecalled effect (auditability)
- memory is advisory-only (no state derivation, invisible body, AUTO reversibility)
- backends are pluggable (InMemoryBackend default)
- the write path derives memory-worthy observations from TaskFailed / settlement
- MemoryRecalled round-trips through the composed effect registry (trace decode)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from shepherd_contexts.memory import (
    InMemoryBackend,
    MemoryBackend,
    MemoryContext,
    MemoryHint,
    MemoryObservation,
    MemoryRecalled,
    observations_from_effects,
)
from shepherd_core.effects.effects import TaskCompleted, TaskFailed
from shepherd_core.types import ReversibilityLevel
from shepherd_runtime.effects.registry import decode_effect

if TYPE_CHECKING:
    from shepherd_core.effects import Effect


# =============================================================================
# Helpers
# =============================================================================


def _hint(title: str, content: str = "", *, digest: str | None = None, hint_type: str = "learning") -> MemoryHint:
    return MemoryHint(title=title, content=content, digest=digest, type=hint_type)  # type: ignore[arg-type]


# =============================================================================
# Read path: recall surfaces hints
# =============================================================================


class TestRecallSurfacesHints:
    def test_create_recalls_matching_hints(self):
        backend = InMemoryBackend(
            [_hint("auth", "use setup-token", digest="d1"), _hint("unrelated", "noise")]
        )
        ctx = MemoryContext.create(backend, query="claude auth", project="shepherd")

        assert len(ctx.hints) == 1
        assert ctx.hints[0].title == "auth"
        assert ctx.hints[0].digest == "d1"

    def test_configure_injects_advisory_addition(self):
        backend = InMemoryBackend([_hint("auth", "use setup-token", digest="d1")])
        ctx = MemoryContext.create(backend, query="auth")

        binding = ctx.configure()

        assert binding.visible is False  # invisible body; hints via additions
        assert len(binding.system_prompt_additions) == 1
        block = binding.system_prompt_additions[0]
        assert "Advisory memory" in block
        assert "use setup-token" in block
        assert "memory:d1" in block  # provenance digest surfaced

    def test_configure_pure_and_stable(self):
        ctx = MemoryContext.create(InMemoryBackend([_hint("a")]), query="a")
        b1 = ctx.configure()
        b2 = ctx.configure()
        assert b1.system_prompt_additions == b2.system_prompt_additions

    def test_empty_query_yields_no_hints(self):
        ctx = MemoryContext.create(InMemoryBackend([_hint("a")]), query="")
        assert ctx.hints == ()
        assert ctx.configure().system_prompt_additions == ()

    def test_none_backend_is_noop(self):
        ctx = MemoryContext.create(None, query="anything")
        assert ctx.hints == ()
        assert ctx.configure().system_prompt_additions == ()
        effs = ctx.extract_effects(None, None)
        assert effs[0].backend == "none"


# =============================================================================
# Read path: the recall is logged as a MemoryRecalled effect
# =============================================================================


class TestRecallIsLogged:
    def test_extract_emits_memory_recalled(self):
        backend = InMemoryBackend(
            [_hint("auth", "use setup-token", digest="d1"), _hint("auth2", "x", digest="d2")]
        )
        ctx = MemoryContext.create(backend, query="auth", project="shepherd")

        effs = ctx.extract_effects(None, None)

        assert len(effs) == 1
        eff = effs[0]
        assert isinstance(eff, MemoryRecalled)
        assert eff.effect_type == "memory_recalled"
        assert eff.query == "auth"
        assert eff.project == "shepherd"
        assert eff.backend == "memory"
        assert eff.hint_count == 2
        assert eff.hint_titles == ("auth", "auth2")
        assert eff.hint_digests == ("d1", "d2")

    def test_decode_round_trips_through_registry(self):
        backend = InMemoryBackend([_hint("auth", "use setup-token", digest="d1")])
        ctx = MemoryContext.create(backend, query="auth", project="shepherd")
        original = ctx.extract_effects(None, None)[0]

        data = original.model_dump(mode="json")
        # The composed registry includes discovered contributors (memory_recalled).
        restored = decode_effect(data)

        assert isinstance(restored, MemoryRecalled)
        assert restored.query == original.query
        assert restored.hint_titles == original.hint_titles
        assert restored.hint_digests == original.hint_digests


# =============================================================================
# Advisory-only guarantees (the reversibility invariant)
# =============================================================================


class TestAdvisoryOnly:
    def test_apply_effect_is_noop(self):
        ctx = MemoryContext.create(InMemoryBackend([_hint("a")]), query="a")
        eff = ctx.extract_effects(None, None)[0]
        # Memory derives no state from effects — applying any effect returns self.
        assert ctx.apply_effect(eff) is ctx

    def test_invisible_in_prompt_body(self):
        ctx = MemoryContext.create(InMemoryBackend([_hint("a")]), query="a")
        assert str(ctx) == ""

    def test_reversibility_is_auto(self):
        ctx = MemoryContext.create(InMemoryBackend(), query="x")
        assert ctx.reversibility == ReversibilityLevel.AUTO

    def test_context_id_is_stable(self):
        a = MemoryContext.create(InMemoryBackend(), query="auth", project="p")
        b = MemoryContext.create(InMemoryBackend(), query="auth", project="p")
        assert a.context_id == b.context_id
        c = MemoryContext.create(InMemoryBackend(), query="other", project="p")
        assert c.context_id != a.context_id


# =============================================================================
# Backend pluggability
# =============================================================================


class TestBackends:
    def test_inmemory_recall_scores_by_term_hits(self):
        backend = InMemoryBackend(
            [
                _hint("auth token", "setup-token works"),
                _hint("auth retry", "refresh needed"),
                _hint("deploy", "unrelated"),
            ]
        )
        hits = backend.recall("auth", n=5)
        assert {h.title for h in hits} == {"auth token", "auth retry"}

    def test_inmemory_save_returns_id_and_records(self):
        backend = InMemoryBackend()
        oid = backend.save(MemoryObservation(title="t", content="c"))
        assert oid is not None
        assert len(backend.saved) == 1
        assert backend.saved[0].title == "t"

    def test_protocol_conformance(self):
        assert isinstance(InMemoryBackend(), MemoryBackend)


# =============================================================================
# Write path: observations from effects
# =============================================================================


class TestObservationsFromEffects:
    def test_task_failed_yields_bugfix_observation(self):
        effects: list[Effect] = [
            TaskFailed(
                task_name="WriteProgram",
                error="permission denied",
                error_type="PermissionError",
                phase="execute",
                last_tool_name="file_write",
                error_location="provider.py:42",
                suggestions=("check grant",),
            )
        ]
        obs = observations_from_effects(effects, project="shepherd")

        assert len(obs) == 1
        assert obs[0].type == "bugfix"
        assert "WriteProgram" in obs[0].title
        assert "PermissionError" in obs[0].title
        assert "permission denied" in obs[0].content
        assert obs[0].topic_key == "failure:PermissionError"
        assert obs[0].project == "shepherd"

    def test_completed_with_discard_yields_anti_pattern(self):
        effects: list[Effect] = [TaskCompleted(task_name="WriteProgram")]
        obs = observations_from_effects(effects, disposition="discard")

        assert len(obs) == 1
        assert obs[0].type == "pattern"
        assert obs[0].topic_key == "discarded:WriteProgram"
        assert "discarded" in obs[0].content

    def test_completed_with_select_yields_decision(self):
        effects: list[Effect] = [TaskCompleted(task_name="WriteProgram")]
        obs = observations_from_effects(effects, disposition="select")

        assert len(obs) == 1
        assert obs[0].type == "decision"
        assert obs[0].topic_key == "select:WriteProgram"

    def test_completed_without_disposition_yields_nothing(self):
        # Without the human's settlement decision, a clean completion is not memory-worthy.
        effects: list[Effect] = [TaskCompleted(task_name="WriteProgram")]
        assert observations_from_effects(effects) == []

    def test_empty_trace_yields_nothing(self):
        assert observations_from_effects([]) == []


# =============================================================================
# Recall limits and empty-query recency
# =============================================================================


class TestRecallLimits:
    def test_max_hints_truncates_recall(self):
        backend = InMemoryBackend([_hint(f"term{i}") for i in range(10)])
        ctx = MemoryContext.create(backend, query="term", max_hints=3)
        assert len(ctx.hints) == 3

    def test_empty_query_returns_most_recent(self):
        backend = InMemoryBackend([_hint("a"), _hint("b"), _hint("c")])
        # No query: surface the last n hints (recency-ish, stable order).
        assert [h.title for h in backend.recall("", n=2)] == ["b", "c"]

    def test_non_matching_query_yields_nothing(self):
        backend = InMemoryBackend([_hint("alpha")])
        ctx = MemoryContext.create(backend, query="zzz-no-match")
        assert ctx.hints == ()
        assert ctx.extract_effects(None, None)[0].hint_count == 0


# =============================================================================
# Mixed write-path effects (M3: multiple completions; M4: nameless completion)
# =============================================================================


class TestWritePathMixed:
    def test_multiple_failures_each_yield_observation(self):
        effects: list[Effect] = [
            TaskFailed(task_name="A", error="e1", error_type="E1"),
            TaskFailed(task_name="A", error="e2", error_type="E2"),
        ]
        obs = observations_from_effects(effects)
        assert len(obs) == 2
        assert all(o.type == "bugfix" for o in obs)

    def test_multiple_completions_each_yield_disposition(self):
        effects: list[Effect] = [
            TaskCompleted(task_name="B"),
            TaskCompleted(task_name="C"),
        ]
        obs = observations_from_effects(effects, disposition="discard")
        assert len(obs) == 2  # M3: no completion silently dropped
        assert all(o.type == "pattern" for o in obs)
        assert {o.topic_key for o in obs} == {"discarded:B", "discarded:C"}

    def test_nameless_completion_falls_back_to_placeholder(self):
        # M4: a TaskCompleted with no task_name still yields an observation.
        effects: list[Effect] = [TaskCompleted()]
        obs = observations_from_effects(effects, disposition="select")
        assert len(obs) == 1
        assert obs[0].topic_key == "select:task"

    def test_mixed_failures_completions_and_disposition(self):
        effects: list[Effect] = [
            TaskFailed(task_name="A", error="boom", error_type="Err"),
            TaskCompleted(task_name="B"),
            TaskCompleted(task_name="B"),  # duplicate name -> two obs (downstream dedupes)
            TaskCompleted(task_name="C"),
        ]
        obs = observations_from_effects(effects, disposition="discard")
        assert len(obs) == 4  # 1 failure + 3 discarded completions
        assert sum(1 for o in obs if o.type == "bugfix") == 1
        assert sum(1 for o in obs if o.type == "pattern") == 3
