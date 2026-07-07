"""B3.2 regressions: recorded-payload parsing stays shape-honest (2132 Bug-2).

The parse layer between a recorded provider payload and `ProviderInvocationResult`
must never let a malformed or partial payload masquerade as structured output:
non-mapping values coerce to {} (never crash, never pass through), a *present*
camelCase key wins over its snake_case twin even when empty (presence-based
fallback, not truthiness), and a plain-text completion carries no structured
output at all. These run offline at the pinned SDK range
(`claude-agent-sdk>=0.1.18,<0.2` — the providers `claude` extra).
"""

from __future__ import annotations

from shepherd_dialect.providers import codex_provider_result_from_payload

_KW = {"provider_id": "codex", "invocation_id": "inv-1", "model": "codex-test"}


def test_present_but_empty_camelcase_key_wins_over_snake_case() -> None:
    # Truthiness `or`-chaining would fall through to the stale snake_case value;
    # presence-based fallback keeps the present key authoritative.
    result = codex_provider_result_from_payload(
        {"structuredOutput": {}, "structured_output": {"stale": "value"}, "finalResponse": "done"},
        **_KW,
    )
    assert result.structured_output == {}
    assert result.output_text == "done"


def test_snake_case_key_used_only_when_camelcase_absent() -> None:
    result = codex_provider_result_from_payload(
        {"structured_output": {"answer": 42}, "finalResponse": "done"},
        **_KW,
    )
    assert result.structured_output == {"answer": 42}


def test_non_mapping_structured_output_coerces_to_empty() -> None:
    for bad in ("a string", 7, ["list"], True):
        result = codex_provider_result_from_payload(
            {"structuredOutput": bad, "finalResponse": "done"},
            **_KW,
        )
        assert result.structured_output == {}, f"non-mapping {bad!r} must coerce, not pass through"


def test_plain_text_completion_has_no_structured_output() -> None:
    result = codex_provider_result_from_payload(
        {"finalResponse": "plain answer"},
        **_KW,
    )
    assert result.structured_output == {}
    assert result.output_text == "plain answer"
