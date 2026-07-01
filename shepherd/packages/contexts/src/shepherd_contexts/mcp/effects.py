"""MCP Server-specific effects."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field
from shepherd_core.effects import Effect

if TYPE_CHECKING:
    from collections.abc import Mapping


class MCPToolCalled(Effect):
    """An MCP server tool was called."""

    effect_type: Literal["mcp_tool_called"] = "mcp_tool_called"
    server_name: str = ""
    tool_name: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    success: bool = True


class MCPServerConnected(Effect):
    """MCP server connection was established.

    Emitted when an MCP server is successfully connected and ready
    to receive tool calls.
    """

    effect_type: Literal["mcp_server_connected"] = "mcp_server_connected"
    server_name: str = ""
    transport_type: str = ""  # "stdio", "sse", "http"
    tools_available: tuple[str, ...] = ()


def get_effect_types() -> Mapping[str, type[Effect]]:
    """Return the explicit effect contributor surface for runtime decode."""
    return {
        "mcp_tool_called": MCPToolCalled,
        "mcp_server_connected": MCPServerConnected,
    }


__all__ = [
    "MCPServerConnected",
    "MCPToolCalled",
    "get_effect_types",
]
