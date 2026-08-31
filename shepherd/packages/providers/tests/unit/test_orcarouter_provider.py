"""Unit tests for OrcaRouterProvider.

Tests the provider identity, defaults, config serialization, and the
inherited OpenAI Responses API agent loop (mocked openai SDK). No API calls.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from shepherd_core.provider import DefaultProviderRuntime
from shepherd_core.types import ProviderBinding, ProviderCapabilities
from shepherd_providers.orcarouter.provider import OrcaRouterProvider


def _make_response(
    response_id: str = "resp_test_123",
    output: list | None = None,
) -> SimpleNamespace:
    """Build a mock openai Response object."""
    return SimpleNamespace(id=response_id, output=output or [], output_text="")


def _make_message(text: str = "Hello") -> SimpleNamespace:
    content = [SimpleNamespace(text=text)]
    return SimpleNamespace(type="message", content=content)


async def _mock_stream_from_response(response: SimpleNamespace):
    """Convert a mock Response into a mock SSE stream."""
    for idx, item in enumerate(response.output):
        yield SimpleNamespace(type="response.output_item.done", item=item, output_index=idx, sequence_number=idx)
    yield SimpleNamespace(type="response.completed", response=response, sequence_number=len(response.output))


def _make_stream_client(*responses: SimpleNamespace) -> MagicMock:
    """Build a mock client whose responses.create returns async streams."""
    streams = iter([_mock_stream_from_response(r) for r in responses])
    mock_client = MagicMock()
    mock_client.responses = MagicMock()
    mock_client.responses.create = AsyncMock(side_effect=lambda **kw: next(streams))
    return mock_client


@pytest.fixture(autouse=True)
def fake_openai_module(monkeypatch):
    """Install a minimal openai module so unit tests stay offline."""

    class BadRequestError(Exception):
        def __init__(self, message: str, *, response=None, body=None):
            super().__init__(message)
            self.response = response
            self.body = body

    class APIError(Exception):
        def __init__(self, message: str, *, request=None, body=None):
            super().__init__(message)
            self.request = request
            self.body = body

    class AsyncOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_module = ModuleType("openai")
    fake_module.BadRequestError = BadRequestError
    fake_module.APIError = APIError
    fake_module.AsyncOpenAI = AsyncOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_module)


@pytest.fixture
def provider():
    return OrcaRouterProvider(name="test", model="openai/gpt-5.5", max_turns=5)


@pytest.fixture
def binding():
    return ProviderBinding(
        context_id="test_ctx",
        capabilities=frozenset({"bash", "read", "write"}),
        trust_level="standard",
    )


class TestProviderIdentity:
    def test_default_model(self):
        provider = OrcaRouterProvider(name="test")
        assert provider.model == "openai/gpt-5.5"

    def test_default_base_url(self):
        provider = OrcaRouterProvider(name="test")
        assert provider.base_url == "https://api.orcarouter.ai/v1"

    def test_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-orca-test-key")
        provider = OrcaRouterProvider(name="test")
        assert provider.api_key == "sk-orca-test-key"

    def test_api_key_override_wins(self, monkeypatch):
        monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-orca-env-key")
        provider = OrcaRouterProvider(name="test", api_key="sk-orca-explicit-key")
        assert provider.api_key == "sk-orca-explicit-key"

    def test_no_api_key_env(self, monkeypatch):
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        provider = OrcaRouterProvider(name="test")
        assert provider.api_key is None

    def test_provider_id_prefix(self, provider):
        assert provider.provider_id.startswith("provider:orcarouter:")

    def test_capabilities_provider_type(self, provider):
        caps = provider.capabilities
        assert isinstance(caps, ProviderCapabilities)
        assert caps.provider_type == "orcarouter"
        assert caps.supports_streaming
        assert caps.supports_tools
        assert caps.supports_structured_output
        assert caps.supports_session


class TestSerialization:
    def test_to_config(self, provider):
        config = provider.to_config()
        assert config["provider_type"] == "orcarouter"
        assert config["model"] == "openai/gpt-5.5"
        assert config["base_url"] == "https://api.orcarouter.ai/v1"
        # api_key must not be serialized into the container config
        assert "api_key" not in config

    def test_from_config_roundtrip(self):
        config = {
            "provider_type": "orcarouter",
            "name": "container",
            "model": "openai/gpt-5.5",
            "base_url": "https://api.orcarouter.ai/v1",
        }
        provider = OrcaRouterProvider.from_config(config)
        assert isinstance(provider, OrcaRouterProvider)
        assert provider.name == "container"
        assert provider.model == "openai/gpt-5.5"

    def test_factory_registered(self):
        from shepherd_runtime.registry import get_provider_factory

        factory = get_provider_factory("orcarouter")
        assert factory is not None
        assert (
            factory({"name": "x", "model": "openai/gpt-5.5", "provider_type": "orcarouter"}).model == "openai/gpt-5.5"
        )


class TestAgentLoop:
    @pytest.mark.asyncio
    async def test_simple_text_response(self, provider, binding):
        """Model returns text, no tool calls — loop exits after 1 turn."""
        scope = MagicMock()
        mock_client = _make_stream_client(_make_response(output=[_make_message("The answer is 42")]))

        with patch("shepherd_providers.openai.provider._get_client", return_value=mock_client):
            result = await provider.execute_sdk(
                "What is 6*7?", binding, DefaultProviderRuntime.from_emitter(scope, task_name="test")
            )

        assert result.success
        assert result.output_text == "The answer is 42"
        assert result.metadata["turns"] == 1
        assert len(result.tool_calls) == 0

    @pytest.mark.asyncio
    async def test_binding_translation_uses_model(self, provider, binding):
        """The Responses API request must carry the OrcaRouter model id."""
        scope = MagicMock()
        mock_client = _make_stream_client(_make_response(output=[_make_message("ok")]))

        with patch("shepherd_providers.openai.provider._get_client", return_value=mock_client):
            await provider.execute_sdk("Hi", binding, DefaultProviderRuntime.from_emitter(scope, task_name="test"))

        create_kwargs = mock_client.responses.create.call_args.kwargs
        assert create_kwargs["model"] == "openai/gpt-5.5"
        assert create_kwargs["stream"] is True
