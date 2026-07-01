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

```bash
pip install shepherd-ai
```

Working on Shepherd itself? Install the local editable closure instead:
`python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt`
(see [CONTRIBUTING.md](https://github.com/shepherd-agents/shepherd/blob/main/CONTRIBUTING.md)).

## Quickstart

Shepherd is an agent framework: a task's implementation can be a sandboxed
agent, and its work comes back as a **reviewable proposal** — nothing touches
your files until you accept it. Here the whole body of a task *is* a Claude
agent.

> Needs the `claude` CLI — signed in (a Claude subscription works) or with an
> `ANTHROPIC_API_KEY`. Neither? Jump to the
> [Offline Quickstart](#offline-quickstart) — it runs anywhere, keyless.

A task is a plain Python function with **no body**; the signature and docstring
are the contract the agent fulfils at runtime:

```python
def write_program(repo, prompt: str, output_path: str = "program.py") -> None:
    """Write a small, self-contained Python program that does what `prompt` asks.

    Save it to output_path. It must run with plain `python3`, read no input,
    and finish on its own within about ten seconds.
    """
```

Set up a scratch workspace and check the agent lane is ready:

```bash
mkdir /tmp/agent-task && cd /tmp/agent-task
shepherd init             # turn this directory into a Shepherd workspace
shepherd doctor claude    # confirm claude CLI, sign-in/key, and sandbox are ready
```

Fetch the demo and let the agent work (about a minute):

```bash
shepherd demo write agent-task > agent_task.py
python agent_task.py
```

The agent writes `donut.py` — but not into your directory. It lands as a
**retained output**: a proposal held safely to one side, which you can run
without applying anything:

```bash
shepherd run changeset --latest --read donut.py | python3 -
```

Ten seconds of spinning ASCII donut, straight out of the retained output. If
you like it, keep it; if not, throw it away — the trace remembers either way
(the demo prints both commands with the real run id):

```bash
shepherd run select <run-ref>     # keep it
shepherd run discard <run-ref>    # ...or not
```

Edit `PROMPT` in `agent_task.py` and re-run to ask for anything else — the
contract stays the same. For an agent that edits existing files, see
`shepherd demo write claude-readme`.

## Offline Quickstart

No API key required. This runs the same retained-output machinery through
Shepherd's deterministic provider — the agent lane above, minus the agent:

```bash
mkdir /tmp/shepherd-quickstart && cd /tmp/shepherd-quickstart

shepherd init                                  # turn this directory into a workspace
shepherd demo write quickstart > quickstart_demo.py
python quickstart_demo.py                      # register + run a task, retaining its result

shepherd run list                              # the run and its status
shepherd run changeset --latest                # what it wrote, kept as a retained output
```

Inspect the full record with `shepherd run show --latest` (add `--json` to any
read command for the durable machine payload); see the
[docs](https://docs.shepherd-agents.ai/) for backend selection and the complete
`run` surface.

## Examples

The demo scripts above are the Python surface in miniature — checked-in copies
live in [`examples/quickstart/`](https://github.com/shepherd-agents/shepherd/tree/main/examples/quickstart).
The visual-artifact notebooks live in
[`examples/notebooks/visual_artifact/notebooks/`](https://github.com/shepherd-agents/shepherd/tree/main/examples/notebooks/visual_artifact/notebooks)
— launch them with `make notebooks`.

## Development

Useful local gates:

```bash
make dev-install
uv run pytest integration-tests/test_quickstart_core.py -q
make baseline
```

## Documentation

Full documentation lives at **[docs.shepherd-agents.ai](https://docs.shepherd-agents.ai/)**. In this repository the docs are authored under [`docs/shepherd/`](https://github.com/shepherd-agents/shepherd/tree/main/docs/shepherd), starting with the
[Quickstart guide](https://github.com/shepherd-agents/shepherd/blob/main/docs/shepherd/start/quickstart.md) and
[Concepts](https://github.com/shepherd-agents/shepherd/blob/main/docs/shepherd/concepts/index.md) — tasks, effects, scopes, permissions, and the trace.

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

This project is licensed under the MIT License — see the [LICENSE](https://github.com/shepherd-agents/shepherd/blob/main/LICENSE) file for details.
