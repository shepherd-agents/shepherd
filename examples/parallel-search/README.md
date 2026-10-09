# Parallel Search MCP

Search the web and fetch page excerpts without a Parallel API key. This example
loads `mcp_servers.json` through Shepherd's `MCPServerContext.from_json`, builds
its provider binding, and runs the configured tools with the Python MCP SDK over
Streamable HTTP. It prints the MCP results as JSON, including source URLs and
excerpts. It calls tools directly; it does not run a model or a Shepherd task.

From the repository root, with Python 3.11+ and uv installed:

```bash
uv sync --all-packages --all-groups
uv pip install --python .venv/bin/python -r examples/parallel-search/requirements.txt
.venv/bin/python examples/parallel-search/search.py "Python asyncio task cancellation" \
  --fetch https://docs.python.org/3/library/asyncio-task.html
```

Omit `--fetch` to search only. The JSON config is opt-in and leaves existing
provider choices unchanged. It sets `type: http` explicitly because the context
loader defaults remote URLs to SSE. Both tools reuse one randomly generated
session identifier per invocation. Requests have a 60-second HTTP timeout and
an overall 90-second deadline; server and tool errors terminate the command.

The [anonymous endpoint](https://docs.parallel.ai/integrations/mcp/search-mcp)
is free for light use at lower rate limits. This configuration sends no
Authorization header and reads no saved provider credentials. Avoid automatic
retry loops when rate limited.

To check the example offline after installation:

```bash
.venv/bin/python -m pytest integration-tests/test_parallel_search_example.py -q
```
