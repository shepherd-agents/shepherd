"""Provider-facing helpers for the runtime-owned container task runner."""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from shepherd_runtime.device.container.provider_registry import (
    create_provider,
    get_provider_factory,
)

if TYPE_CHECKING:
    from shepherd_core.provider import Provider

logger = logging.getLogger(__name__)


class ProviderNotAvailableError(Exception):
    """Raised when a requested provider cannot be loaded."""


class _MockProvider:
    """Mock provider for testing when real providers aren't available."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self._id = f"mock-{os.getpid()}"

    @property
    def provider_id(self) -> str:
        return f"provider:mock:{self._id}"

    async def execute_sdk(
        self,
        prompt: str,
        binding: Any,
        runtime: Any,
        hooks: dict | None = None,
    ) -> Any:
        """Mock execution that returns a placeholder result."""
        from shepherd_core.types import ExecutionResult

        return ExecutionResult(
            success=True,
            output_text=f"[Mock execution] Prompt: {prompt[:100]}...",
            metadata={
                "provider": "mock",
                "config": self.config,
            },
        )


def _create_provider(config: dict[str, Any]) -> Provider:
    """Create provider from config using the runtime-owned provider registry."""
    provider_type = config.get("provider_type")

    if provider_type in {"claude", "openai", "orcarouter"}:
        module_name = f"shepherd_providers.{provider_type}"
        if module_name in sys.modules and sys.modules[module_name] is None:
            err = ImportError(f"{module_name} is unavailable")
            raise ProviderNotAvailableError(
                f"Provider '{provider_type}' requested but shepherd-providers is not installed. "
                f"Install with: pip install shepherd-providers\n"
                f"Original error: {err}"
            ) from err

    if provider_type and get_provider_factory(provider_type):
        return create_provider(config)

    if provider_type == "claude":
        try:
            from shepherd_providers.claude import ClaudeProvider  # type: ignore[import-not-found,unused-ignore]

            return ClaudeProvider(  # type: ignore[no-any-return,unused-ignore]
                name=config.get("name", "container"),
                model=config.get("model", "claude-sonnet-4-20250514"),
            )
        except ImportError as e:
            raise ProviderNotAvailableError(
                f"Provider 'claude' requested but shepherd-providers is not installed. "
                f"Install with: pip install shepherd-providers\n"
                f"Original error: {e}"
            ) from e

    if provider_type == "openai":
        try:
            from shepherd_providers.openai import OpenAIProvider  # type: ignore[import-not-found,unused-ignore]

            return OpenAIProvider(  # type: ignore[no-any-return,unused-ignore]
                name=config.get("name", "container"),
                model=config.get("model", "gpt-4"),
            )
        except ImportError as e:
            raise ProviderNotAvailableError(
                f"Provider 'openai' requested but shepherd-providers is not installed. "
                f"Install with: pip install shepherd-providers\n"
                f"Original error: {e}"
            ) from e

    if provider_type == "orcarouter":
        try:
            from shepherd_providers.orcarouter import OrcaRouterProvider  # type: ignore[import-not-found,unused-ignore]

            return OrcaRouterProvider.from_config(config)  # type: ignore[no-any-return,unused-ignore]
        except ImportError as e:
            raise ProviderNotAvailableError(
                f"Provider 'orcarouter' requested but shepherd-providers is not installed. "
                f"Install with: pip install shepherd-providers\n"
                f"Original error: {e}"
            ) from e

    if provider_type == "opencode":
        try:
            from shepherd_providers.opencode import OpenCodeProvider  # type: ignore[import-not-found,unused-ignore]

            return OpenCodeProvider.from_config(config)  # type: ignore[no-any-return,unused-ignore]
        except ImportError as e:
            raise ProviderNotAvailableError(
                f"Provider 'opencode' requested but shepherd-providers is not installed. "
                f"Install with: pip install shepherd-providers[opencode]\n"
                f"Original error: {e}"
            ) from e

    if provider_type == "mock" or provider_type is None:
        logger.info("Using mock provider (provider_type=%r)", provider_type)
        return _MockProvider(config)  # type: ignore[return-value]

    raise ProviderNotAvailableError(f"Unknown provider type: {provider_type!r}")


def _validate_session_resumable(session_id: str | None, cwd: str | None) -> str | None:
    """Validate that a session transcript exists before attempting resume."""
    if not session_id:
        return None

    from shepherd_core.types import compute_transcript_path

    transcript_path = compute_transcript_path(cwd, session_id)

    if not Path(transcript_path).exists():
        logger.warning(
            "Transcript missing for session %s... at %s, starting fresh session instead",
            session_id[:12],
            transcript_path,
        )
        return None

    logger.debug("Transcript validated for session %s... at %s", session_id[:12], transcript_path)
    return session_id


def _build_binding_from_contexts(
    contexts: dict[str, Any],
    tools: list[str] | None,
    output_format: dict[str, Any] | None = None,
) -> Any:
    """Build a ProviderBinding from deserialized container contexts."""
    from shepherd_core.types import ProviderBinding

    if not contexts:
        return None

    cwd = None
    capabilities: set[str] = set()
    context_descriptions: list[str] = []
    session_id: str | None = None
    session_isolation: str = "isolated"

    for binding_name, state in contexts.items():
        path = None
        if hasattr(state, "path"):
            path = state.path
            cwd = path
        elif isinstance(state, dict) and "path" in state:
            path = state["path"]
            cwd = path

        state_caps: frozenset[str] = frozenset()
        if hasattr(state, "capabilities"):
            state_caps = frozenset(state.capabilities)
            capabilities.update(state_caps)
        elif isinstance(state, dict) and "capabilities" in state:
            state_caps = frozenset(state.get("capabilities", []))
            capabilities.update(state_caps)

        if path:
            access = sorted(state_caps) if state_caps else ["read", "write"]
            desc = f"Git workspace at `{path}` ({', '.join(access)} access)"
            pending = None
            if hasattr(state, "pending_patches"):
                pending = state.pending_patches
            elif isinstance(state, dict) and "pending_patches" in state:
                pending = state.get("pending_patches", [])
            if pending:
                desc += f"\n{len(pending)} pending patches from prior steps."
            context_descriptions.append(desc)

        ctx_type = getattr(state, "context_type", None)
        if ctx_type is None and isinstance(state, dict):
            ctx_type = state.get("context_type")

        if binding_name == "session" or ctx_type == "session":
            if hasattr(state, "session_id"):
                session_id = state.session_id
            elif isinstance(state, dict):
                session_id = state.get("session_id")

            if session_id:
                session_isolation = "forked"

    context_description = "\n\n".join(context_descriptions) if context_descriptions else None
    final_capabilities = frozenset(capabilities) if capabilities else frozenset({"read", "write"})

    return ProviderBinding(
        context_id="container-execution",
        context_type="ContainerContext",
        context_description=context_description,
        cwd=cwd,
        capabilities=final_capabilities,
        output_format=output_format,
        session_id=session_id,
        session_isolation=session_isolation,  # type: ignore[arg-type]
    )


def _enforce_container_session_invariants(binding: Any, provider_config: dict[str, Any] | None = None) -> Any:
    """Apply container-session invariants to an already-built binding."""
    if binding and binding.session_id and binding.session_isolation != "forked":
        logger.warning(
            "Overriding session_isolation from %r to 'forked' (container execution invariant)",
            binding.session_isolation,
        )
        binding = dataclass_replace(binding, session_isolation="forked")

    # Only applies to Claude — its sessions are stored as transcript files
    # on disk at ~/.claude/projects/. Other providers (OpenCode, etc.) store
    # sessions server-side and this check would always fail for them.
    _pconfig = provider_config or {}
    if binding and binding.session_id and _pconfig.get("provider_type") == "claude":
        validated_session_id = _validate_session_resumable(binding.session_id, binding.cwd)
        if validated_session_id != binding.session_id:
            binding = dataclass_replace(
                binding,
                session_id=None,
                session_isolation="isolated",
            )
            logger.info("Falling back to fresh session due to missing transcript")

    return binding


def _serialize_execution_result(result: Any) -> dict[str, Any]:
    """Serialize ExecutionResult to a JSON-compatible dictionary."""
    if hasattr(result, "model_dump"):
        return result.model_dump()  # type: ignore[no-any-return]
    if hasattr(result, "__dict__"):
        data = {}
        for key, value in result.__dict__.items():
            if key.startswith("_"):
                continue
            if hasattr(value, "model_dump"):
                data[key] = value.model_dump()
            elif isinstance(value, (list, tuple)):
                data[key] = [v.model_dump() if hasattr(v, "model_dump") else v for v in value]
            else:
                data[key] = value
        return data
    return {"success": result.success if hasattr(result, "success") else True}


__all__ = [
    "ProviderNotAvailableError",
    "_MockProvider",
    "_build_binding_from_contexts",
    "_create_provider",
    "_enforce_container_session_invariants",
    "_serialize_execution_result",
    "_validate_session_resumable",
]
