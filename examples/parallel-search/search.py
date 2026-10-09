"""Run Parallel Search MCP tools using Shepherd's JSON context configuration."""
# ruff: noqa: INP001 -- standalone executable example

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from shepherd_contexts.mcp import MCPServerContext

CONFIG = Path(__file__).with_name("mcp_servers.json")


async def search(query, fetch_url=None):
    context = MCPServerContext.from_json(CONFIG)["parallel_search"]
    config = context.configure().mcp_servers[context.name]
    session_id = uuid4().hex
    output = {}
    # The example owns this client; all MCP requests use the configured headers.
    async with (
        asyncio.timeout(90),
        httpx.AsyncClient(headers=config["headers"], timeout=60) as client,
        streamable_http_client(config["url"], http_client=client) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        available = {tool.name for tool in (await session.list_tools()).tools}
        calls = [("web_search", {"objective": query, "search_queries": [query], "session_id": session_id})]
        if fetch_url:
            calls.append(("web_fetch", {"urls": [fetch_url], "session_id": session_id}))
        for name, arguments in calls:
            if name not in available or name not in config["allowed_tools"]:
                raise RuntimeError(f"MCP tool unavailable or not allowed: {name}")
            result = await session.call_tool(name, arguments)
            if result.isError:
                raise RuntimeError(f"{name} failed: {result.content}")
            output[name] = result.model_dump(mode="json", exclude_none=True)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", help="A short keyword query, ideally 3-6 words")
    parser.add_argument("--fetch", metavar="URL", help="Also fetch excerpts from this page")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(search(args.query, args.fetch)), indent=2))
