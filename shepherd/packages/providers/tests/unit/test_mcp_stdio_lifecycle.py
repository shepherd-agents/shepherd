"""Spike 2, Step 4: Lifecycle tests for StdioMCPBridge.

Tests the bridge module against real MCP server subprocesses using
``mcp.server.fastmcp.FastMCP`` inline server scripts.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap

import pytest

try:
    from shepherd_providers.openai._mcp_stdio_bridge import StdioMCPBridge, mcp_tools_to_function_schemas

    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False

pytestmark = pytest.mark.skipif(not _MCP_AVAILABLE, reason="mcp package not installed")

# ---------------------------------------------------------------------------
# Inline MCP server script
# ---------------------------------------------------------------------------

_SERVER_SCRIPT = textwrap.dedent("""\
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test-bridge-server")

    @mcp.tool()
    def echo(message: str) -> str:
        \"\"\"Echo the message back.\"\"\"
        return f"echo: {message}"

    @mcp.tool()
    def add(a: int, b: int) -> str:
        \"\"\"Add two numbers.\"\"\"
        return str(a + b)

    @mcp.tool()
    def fail(reason: str) -> str:
        \"\"\"Always raises an error.\"\"\"
        raise ValueError(f"intentional: {reason}")

    if __name__ == "__main__":
        mcp.run(transport="stdio")
""")


@pytest.fixture
def server_script(tmp_path) -> str:
    """Write the server script to a temp file and return its path."""
    script = tmp_path / "bridge_server.py"
    script.write_text(_SERVER_SCRIPT)
    return str(script)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    async def test_start_list_tools_call_stop(self, server_script):
        """Full lifecycle: start -> list tools -> call tool -> stop."""
        bridge = StdioMCPBridge()
        try:
            await bridge.start(sys.executable, [server_script])

            # Tools discovered
            tools = bridge.tools
            names = {t.name for t in tools}
            assert "echo" in names
            assert "add" in names
            assert "fail" in names

            # is_alive should be True
            assert bridge.is_alive

            # Call a tool
            result = await bridge.call_tool("echo", {"message": "hello"})
            assert not result.isError
            assert result.content[0].text == "echo: hello"

            # Call another tool
            result = await bridge.call_tool("add", {"a": 3, "b": 4})
            assert not result.isError
            assert result.content[0].text == "7"
        finally:
            await bridge.stop()

        # After stop
        assert not bridge.is_alive

    async def test_context_manager(self, server_script):
        """Bridge can be used as async context manager."""
        async with StdioMCPBridge() as bridge:
            await bridge.start(sys.executable, [server_script])
            assert bridge.is_alive
            result = await bridge.call_tool("echo", {"message": "ctx"})
            assert result.content[0].text == "echo: ctx"

        # Exiting context manager should stop the bridge
        assert not bridge.is_alive

    async def test_tools_converted_to_openai_format(self, server_script):
        """Tools discovered by the bridge can be converted to OpenAI format."""
        async with StdioMCPBridge() as bridge:
            await bridge.start(sys.executable, [server_script])
            schemas = mcp_tools_to_function_schemas(bridge.tools, "test")
            assert len(schemas) >= 3
            names = {s["function"]["name"] for s in schemas}
            assert "mcp__test__echo" in names
            assert "mcp__test__add" in names


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    async def test_start_invalid_command(self):
        """Starting with a non-existent command should raise an error."""
        bridge = StdioMCPBridge()
        with pytest.raises(FileNotFoundError):
            await bridge.start("/nonexistent/binary/xyz", ["--bad"])
        # Cleanup should be safe even after failed start
        await bridge.stop()

    async def test_call_tool_with_invalid_arguments(self, server_script):
        """Calling a tool with wrong arguments should return an MCP error."""
        async with StdioMCPBridge() as bridge:
            await bridge.start(sys.executable, [server_script])
            # 'echo' expects {"message": str} -- pass wrong shape
            result = await bridge.call_tool("echo", {"wrong_param": 123})
            # The MCP server should report this as an error
            assert result.isError

    async def test_call_tool_that_raises(self, server_script):
        """Tool that raises an exception should return isError=True."""
        async with StdioMCPBridge() as bridge:
            await bridge.start(sys.executable, [server_script])
            result = await bridge.call_tool("fail", {"reason": "test"})
            assert result.isError
            assert "intentional" in result.content[0].text

    async def test_call_tool_before_start(self):
        """Calling a tool before start should raise RuntimeError."""
        bridge = StdioMCPBridge()
        with pytest.raises(RuntimeError, match="not started"):
            await bridge.call_tool("echo", {"message": "hi"})


# ---------------------------------------------------------------------------
# Concurrent tool calls
# ---------------------------------------------------------------------------


class TestConcurrentCalls:
    async def test_two_concurrent_calls(self, server_script):
        """Two concurrent call_tool() invocations should both succeed."""
        async with StdioMCPBridge() as bridge:
            await bridge.start(sys.executable, [server_script])

            task1 = asyncio.create_task(bridge.call_tool("echo", {"message": "first"}))
            task2 = asyncio.create_task(bridge.call_tool("echo", {"message": "second"}))

            r1, r2 = await asyncio.gather(task1, task2)

            assert not r1.isError
            assert not r2.isError
            texts = {r1.content[0].text, r2.content[0].text}
            assert texts == {"echo: first", "echo: second"}

    async def test_concurrent_mixed_success_failure(self, server_script):
        """Concurrent calls where one succeeds and one fails."""
        async with StdioMCPBridge() as bridge:
            await bridge.start(sys.executable, [server_script])

            task_ok = asyncio.create_task(bridge.call_tool("echo", {"message": "ok"}))
            task_fail = asyncio.create_task(bridge.call_tool("fail", {"reason": "concurrent"}))

            r_ok, r_fail = await asyncio.gather(task_ok, task_fail)

            assert not r_ok.isError
            assert r_ok.content[0].text == "echo: ok"
            assert r_fail.isError
            assert "concurrent" in r_fail.content[0].text


# ---------------------------------------------------------------------------
# is_alive semantics
# ---------------------------------------------------------------------------


class TestIsAlive:
    async def test_is_alive_before_start(self):
        """Before start, is_alive should be False."""
        bridge = StdioMCPBridge()
        assert not bridge.is_alive

    async def test_is_alive_after_start(self, server_script):
        """After start, is_alive should be True."""
        bridge = StdioMCPBridge()
        try:
            await bridge.start(sys.executable, [server_script])
            assert bridge.is_alive
        finally:
            await bridge.stop()

    async def test_is_alive_after_stop(self, server_script):
        """After stop, is_alive should be False."""
        bridge = StdioMCPBridge()
        await bridge.start(sys.executable, [server_script])
        await bridge.stop()
        assert not bridge.is_alive

    async def test_double_stop_is_safe(self, server_script):
        """Calling stop twice should not raise."""
        bridge = StdioMCPBridge()
        await bridge.start(sys.executable, [server_script])
        await bridge.stop()
        await bridge.stop()  # Should not raise
        assert not bridge.is_alive
