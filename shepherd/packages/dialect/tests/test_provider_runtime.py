from __future__ import annotations

from shepherd_dialect.provider_runtime import (
    DEFAULT_TEXT_EXCERPT_LIMIT,
    ProviderInvocationResult,
    provider_invocation_outcome,
    redacted_text_payload,
)


def test_provider_text_payload_retains_up_to_ten_thousand_characters() -> None:
    text = "a" * (DEFAULT_TEXT_EXCERPT_LIMIT + 500)

    payload = redacted_text_payload(text, field="text")

    assert payload["text_length"] == len(text)
    assert payload["text_excerpt"] == text[-DEFAULT_TEXT_EXCERPT_LIMIT:]


def test_provider_outcome_uses_the_shared_text_retention_limit() -> None:
    text = "prefix" + "b" * DEFAULT_TEXT_EXCERPT_LIMIT
    result = ProviderInvocationResult(output_text=text)

    outcome = provider_invocation_outcome(result, provider_id="fixture", invocation_id="fixture:1")

    assert outcome["output_text_excerpt"] == text[-DEFAULT_TEXT_EXCERPT_LIMIT:]
