# shepherd-providers

Provider implementations for the Shepherd framework.

## Installation

```bash
pip install shepherd-providers[claude]  # For Claude provider
pip install shepherd-providers[all]     # All providers
```

## Usage

```python
from shepherd_providers.claude import ClaudeProvider
from shepherd_runtime.scope import Scope

provider = ClaudeProvider(name="default", model="claude-sonnet-4-20250514")

with Scope() as scope:
    scope.register_provider("default", provider, default=True)
    # ... execute tasks
```

## Providers

- **ClaudeProvider**: Claude Agent SDK adapter
- **OpenAIProvider**: OpenAI Agents SDK adapter
- **OrcaRouterProvider**: OrcaRouter OpenAI-compatible gateway adapter

### OrcaRouterProvider

[OrcaRouter](https://www.orcarouter.ai) is an OpenAI-compatible AI gateway
for models and agents. It exposes a provider/model namespace across many
models at `https://api.orcarouter.ai/v1`, with adaptive routing, automatic
failover, and zero-markup inference behind the same endpoint.

```python
from shepherd_providers.orcarouter import OrcaRouterProvider

provider = OrcaRouterProvider(name="default", model="openai/gpt-5.5")
```

The API key is read from the `ORCAROUTER_API_KEY` environment variable (or
passed via the `api_key` argument). Model IDs are namespaced, e.g.
`orcarouter/auto` for the smart router or `openai/gpt-5.5` for a pinned model.
