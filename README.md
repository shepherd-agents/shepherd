<div align="center">
<img src="https://shepherd-agents.ai/assets/logo-shepherd.png" alt="Shepherd" width="140">
<h1>Shepherd: Programmable Meta-Agents via Reversible Execution Traces</h1>

![Status: Alpha](https://img.shields.io/badge/status-alpha-orange?style=for-the-badge) [![PyPI](https://img.shields.io/pypi/v/shepherd-ai?style=for-the-badge&logo=pypi&logoColor=white)](https://pypi.org/project/shepherd-ai/) [![Python](https://img.shields.io/pypi/pyversions/shepherd-ai?style=for-the-badge&logo=python&logoColor=white&label=)](https://pypi.org/project/shepherd-ai/) [![Homepage](https://img.shields.io/badge/Homepage-4d8cd8?style=for-the-badge&logo=google-chrome&logoColor=white)](https://shepherd-agents.ai/) [![Docs](https://img.shields.io/badge/Docs-4d8cd8?style=for-the-badge&logo=materialformkdocs&logoColor=white)](https://docs.shepherd-agents.ai/) [![Paper](https://img.shields.io/badge/Paper-2605.10913-red?style=for-the-badge)](https://arxiv.org/abs/2605.10913) [![Blog](https://img.shields.io/badge/Blog-4d8cd8?style=for-the-badge)](https://shepherd-agents.ai/blog)
</div>

---

> [!IMPORTANT]
> **Shepherd is in early alpha** and under active development.
> APIs may still change between releases. Feedback and issues are very welcome!

<p align="center">
  <a href="#installation">Install</a> |
  <a href="#quickstart">Quickstart</a> |
  <a href="#examples">Examples</a> |
  <a href="https://docs.shepherd-agents.ai/">Docs</a> |
  <a href="#citation">Citation</a>
</p>

**Shepherd** is a runtime substrate for agent work that needs inspection,
reversibility, and supervision. It records agent runs as durable, inspectable
execution traces, with retained workspace outputs that can be reviewed before
they are selected, released, or discarded.

## Installation

Once the public package is published:

```bash
pip install shepherd-ai
```

For the current pre-launch checkout, install the local editable closure from the
repository root:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
```

The Python import is:

```python
import shepherd as sp
```

The CLI is:

```bash
shepherd --help
```

## Quickstart

Create a scratch project, initialize Shepherd's workspace-control substrate, run
a deterministic retained-output demo, and inspect the resulting trace:

```bash
mkdir /tmp/shepherd-quickstart
cd /tmp/shepherd-quickstart

shepherd init --backend auto
shepherd demo write quickstart > quickstart_demo.py
python quickstart_demo.py

shepherd run list
shepherd run show --latest
shepherd run trace --latest --events
shepherd run changeset --latest
```

The demo registers a task, runs it through Shepherd's workspace-control world
channel, stores the result as a retained workspace output, and releases that
output explicitly. Add `--json` to read commands when you need the durable
machine payload:

```bash
shepherd run show --latest --json
```

`--backend auto` asks Shepherd to select the available workspace carrier. If you
need to be explicit, use `--backend clonefile` on macOS or `--backend fuse` /
`--backend kernel` on Linux.

## Python Surface

Shepherd's day-one public Python surface is deliberately small:

```python
import shepherd as sp


@sp.task
def draft_release_note(component: str, change: str) -> str:
    return f"{component}: {change}"


sp.workspace(model={"name": "offline-demo"}, root=".")
print(draft_release_note("world channel", "retained outputs are inspectable"))
```

For provenance-backed workspace runs, open an initialized workspace:

```python
import shepherd as sp

workspace = sp.open(".")
try:
    repo = workspace.git_repo()
    # Register and run workspace-control tasks against repo handles.
finally:
    workspace.close()
```

## Optional Claude Lane

On a host with native jail support, the local `claude` CLI, and
`ANTHROPIC_API_KEY`:

```bash
shepherd doctor claude --backend auto
shepherd demo write claude-readme > claude_readme.py
python claude_readme.py
```

This lane uses `runtime={"provider": "claude"}` through the same retained-output
workspace-control path. It is optional and not required for the deterministic
quickstart.

## Examples

Checked-in quickstart examples live in:

- [`examples/quickstart/offline_task.py`](examples/quickstart/offline_task.py)
- [`examples/quickstart/world_channel.py`](examples/quickstart/world_channel.py)
- [`examples/quickstart/claude_readme.py`](examples/quickstart/claude_readme.py)

The visual-artifact notebooks live in:

- [`examples/notebooks/visual_artifact/notebooks/`](examples/notebooks/visual_artifact/notebooks/)

Launch them with:

```bash
make notebooks
```

## Development

Useful local gates:

```bash
make dev-install
uv run pytest integration-tests/test_quickstart_core.py -q
make baseline
```

## Documentation

Full documentation lives at **[docs.shepherd-agents.ai](https://docs.shepherd-agents.ai/)**. In this repository the docs are authored under [`docs/shepherd/`](docs/shepherd/):

- [Quickstart guide](docs/shepherd/start/quickstart.md)
- [Runtime substrate and the world channel](docs/shepherd/concepts/runtime-substrate.md)
- [Concepts](docs/shepherd/concepts/index.md) — tasks, effects, scopes, permissions, and the trace
- [Examples](examples/README.md)

## Reproducing Paper Results

The full experiment code — the meta-agent applications and the framework-performance microbenchmarks — lives in a companion repository: **[shepherd-agents/shepherd-experiments](https://github.com/shepherd-agents/shepherd-experiments)**. It bundles the frozen substrate snapshot used for the paper, so the numbers stay reproducible against the exact version that produced them.

## Citation

```bibtex
@misc{yu2026shepherdenablingprogrammablemetaagents,
      title={Shepherd: Enabling Programmable Meta-Agents via Reversible Agentic Execution Traces},
      author={Simon Yu and Derek Chong and Ananjan Nandi and Dilara Soylu and Jiuding Sun and Christopher D Manning and Weiyan Shi},
      year={2026},
      eprint={2605.10913},
      archivePrefix={arXiv},
      primaryClass={cs.AI},
      url={https://arxiv.org/abs/2605.10913},
}
```

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
