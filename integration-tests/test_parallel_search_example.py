"""Exercise the example's loader and real MCP transport without network access."""

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "parallel-search" / "search.py"


def load_example():
    """Import the standalone example without invoking its CLI."""
    spec = importlib.util.spec_from_file_location("parallel_search_example", EXAMPLE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
@pytest.mark.parametrize("fetch_url", [None, "https://docs.python.org/3/library/asyncio-task.html"])
async def test_loader_transport_and_tools(monkeypatch, fetch_url):
    """Observe configured headers and tool dispatch through the real MCP SDK."""
    example = load_example()
    requests = []
    calls = []

    def respond(request):
        requests.append(request)
        assert str(request.url) == "https://search.parallel.ai/mcp"
        assert request.headers["User-Agent"] == "shepherd-parallel-search-example/1.0"
        assert "Authorization" not in request.headers
        payload = json.loads(request.content)
        method = payload["method"]
        if "id" not in payload:
            return httpx.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": payload["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "example-fixture", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [{"name": name, "inputSchema": {"type": "object"}} for name in ("web_search", "web_fetch")]
            }
        else:
            assert method == "tools/call"
            calls.append(payload["params"])
            result = {"content": [{"type": "text", "text": "Python documentation excerpt"}], "isError": False}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        example.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            **kwargs,
            transport=httpx.MockTransport(respond),
        ),
    )
    # Keys present in the environment must not turn this config into paid access.
    monkeypatch.setenv("PARALLEL_API_KEY", "unused-fixture-key")
    output = await example.search("Python asyncio task cancellation", fetch_url)
    expected = ["web_search"] + (["web_fetch"] if fetch_url else [])
    assert list(output) == expected
    assert [call["name"] for call in calls] == expected
    assert all(output[name]["content"][0]["text"] == "Python documentation excerpt" for name in expected)
    assert calls[0]["arguments"]["search_queries"] == ["Python asyncio task cancellation"]
    session_id = calls[0]["arguments"]["session_id"]
    assert len(session_id) == 32
    if fetch_url:
        assert calls[1]["arguments"] == {"urls": [fetch_url], "session_id": session_id}
    assert len(requests) == 3 + len(expected)


@pytest.mark.asyncio
async def test_tool_error_is_not_reported_as_success(monkeypatch):
    """Propagate MCP tool errors to the command caller."""
    example = load_example()

    class Session:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def initialize(self):
            pass

        async def list_tools(self):
            from mcp.types import ListToolsResult, Tool

            return ListToolsResult(tools=[Tool(name="web_search", inputSchema={"type": "object"})])

        async def call_tool(self, *args):
            from mcp.types import CallToolResult, TextContent

            return CallToolResult(content=[TextContent(type="text", text="rate limited")], isError=True)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def transport(*args, **kwargs):
        yield None, None, None

    monkeypatch.setattr(example, "ClientSession", Session)
    monkeypatch.setattr(example, "streamable_http_client", transport)
    with pytest.raises(RuntimeError, match=r"web_search failed.*rate limited"):
        await example.search("Python asyncio task cancellation")
