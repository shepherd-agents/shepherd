# Shepherd

Effect-based framework for building AI agents with multi-provider support.

This package is the public facade for the Shepherd workspace. The top-level
`shepherd` module exposes the first-run surface: offline function-form tasks,
workspace-control handles, retained outputs, and explicit run inspection.

## Installation

The current pre-launch package set is installed from a checkout. Published PyPI
names are a release gate because parts of the internal dependency closure are
not safely namespaced yet.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
```

## Five-Minute Quickstart

Use `import shepherd as sp` and the `sp` CLI:

```bash
mkdir /tmp/shepherd-quickstart
cd /tmp/shepherd-quickstart
sp init
sp demo write quickstart > quickstart_demo.py
python quickstart_demo.py
sp run list
sp run show --latest
sp run trace --latest --events
```

The deterministic demo registers a provider-owned task, runs it through the
workspace-control world channel, inspects the retained output, and releases it
explicitly.

The offline task nucleus is still available for pure Python examples:

```python
from dataclasses import dataclass

import shepherd as sp


@dataclass(frozen=True)
class DemoModel:
    name: str = "offline-demo"


@sp.task
def draft_release_note(component: str, change: str) -> str:
    return f"{component}: {change}"


sp.workspace(model=DemoModel(), root=".")
print(draft_release_note("world channel", "retained outputs are inspectable"))
```

For a configured jail-capable host, check the optional live lane:

```bash
sp doctor claude
sp demo write claude-readme > claude_readme.py
python claude_readme.py
```

That lane uses `runtime={"provider": "claude"}`, the local `claude` CLI,
`ANTHROPIC_API_KEY`, and native jail placement. It does not reuse a desktop
login.

See [the guide quickstart](../../docs/guides/quickstart.md) for the full
first-run walkthrough and [`examples/quickstart`](../../../examples/quickstart)
for checked-in executable examples.

## Public API Shape

- `workspace(model=..., root=...)` installs the ambient offline task workspace.
- `open(cwd=".")` opens an initialized workspace-control repository.
- Function-form `@task` defines local structured work.
- `WorkspaceTask`/`ShepherdWorkspace.run(...)` run provider-owned tasks against
  selected `GitRepo` values.
- `RunOutput.changeset()` and `select` / `release` / `discard` expose explicit
  retained-output settlement.
- `shepherd.effects` is a narrow submodule for `Ask`, `Tell`, and
  `Resumption` base classes.
- Advanced scope, provider, context, workflow, device, export, and domain APIs
  stay under owner modules such as `shepherd_runtime.scope`,
  `shepherd_providers`, `shepherd_contexts`, and `shepherd.pipeline`.

## Offline And Test Usage

Use `examples/quickstart/offline_task.py` when you want to exercise task
plumbing without a live provider or `.vcscore` workspace.

## Docs

Current first-run docs:

- [Quickstart](../../docs/guides/quickstart.md)
- [Core Concepts](../../docs/guides/core-concepts.md)
- [`syntax_nucleus.py`](../../examples/tutorials/syntax_nucleus.py)
- [Project Status](../../docs/status.md)

The broader guide tree still contains migration reference material for legacy
class-form tasks, global scope helpers, devices, pipelines, and examples. Those
pages are useful while the owner-path migrations continue, but they are not the
day-1 public facade route.

## Package Structure

The top-level facade imports only the callable-spine names. Broader owner
packages remain importable from their owner paths when installed directly or via
the relevant extras:

- `shepherd_runtime` - scope, task execution, devices, steps, and lifecycle.
- `shepherd_providers` - optional provider implementations.
- `shepherd_contexts` - optional stateful contexts such as sessions and
  workspaces.
- `shepherd_export` - trajectory export and import.

## Development

From the repository root:

```bash
uv sync --all-packages
uv run pytest shepherd/packages/meta/tests
```

## License

MIT
