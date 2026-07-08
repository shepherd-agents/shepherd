<div align="center">
<img src="https://shepherd-agents.ai/assets/logo-shepherd.svg" alt="Shepherd" width="140">
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
  <a href="#permissions-the-signature-is-the-permission-surface">Permissions</a> |
  <a href="#examples">Examples</a> |
  <a href="https://docs.shepherd-agents.ai/">Docs</a> |
  <a href="#citation">Citation</a>
</p>

**Shepherd** is a runtime substrate for agent work that needs inspection,
reversibility, and supervision. It records agent runs as durable, inspectable
execution traces, with retained workspace outputs that can be reviewed before
they are selected, applied, released, or discarded.

> **Platforms.** Shepherd requires **Python 3.11+**. OS-level grant enforcement
> is exercised on **macOS** (Seatbelt) today; on **Linux**, Landlock enforcement
> is container-gated. **Windows is unsupported** (enforcement would be
> advisory-only at best) — use **WSL**.

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
>
> On a subscription, a sandboxed run is most reliable with a long-lived token:
> `export CLAUDE_CODE_OAUTH_TOKEN=$(claude setup-token)`. A short-lived signed-in
> session can't be refreshed from inside the sandbox, so it may work
> interactively yet fail here — `shepherd doctor claude` (add `--probe` for a real
> auth round-trip under Shepherd's config, in the parent — not a jailed run) tells
> you which credential you have before you run. If Claude returns an org-policy
> error (HTTP 403), that's an account/organization limit, not a login problem — a
> different key or your org admin is the fix. And an outright `claude` CLI hang
> (e.g. a stale version) surfaces as a budget timeout, not an auth error.

A task is a plain Python function with **no body**; the signature and docstring
are the contract the agent fulfils at runtime — including its permissions: the
grant on `repo` is what lets the agent write the repository
(see [Permissions](#permissions-the-signature-is-the-permission-surface)):

```python
def write_program(
    repo: sp.GitRepo,
    prompt: str,
    output_path: str = "program.py",
) -> None:
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
shepherd run apply  <run-ref>     # ...or merge it onto a workspace that moved on
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

## Permissions: the signature is the permission surface

A task can declare a read-only or read-write grant **per bound repository**, in
its signature:

```python
from shepherd import task, May, GitRepo, ReadOnly, ReadWrite

@task
def apply_documented_fix(
    docs:    May[GitRepo, ReadOnly],   # read-only: writes refused at the OS
    backend: May[GitRepo, ReadWrite],  # writable root
    issue:   str,
) -> None: ...
```

On a jailed device the grant is compiled to that run's writable roots and
**enforced at the native syscall jail** (macOS Seatbelt; Linux Landlock): a
write to a `ReadOnly`-granted repository, or to any managed path not covered by
a `ReadWrite` grant, is refused at the syscall — before the last undo point, not
advised and not caught only at a merge gate. Reading the signature *is* reading
the permission surface, and `shepherd task show` renders it expanded. Grants are
whole-profile per binding (a bound repository is entirely writable or entirely
read-only). Bindings are named with `ws.bind(root="backend/", name="backend")`
and passed to a run with `workspace.run(task, bindings={...})`; each run's world
output is inspected per binding with `run.changeset(name="backend")` and settled
once with `select` / `apply` / `release` / `discard` (`apply` three-way-merges a
candidate onto a workspace that already moved on, when their changes are
path-disjoint).

> **Scope (P-030 v0.2).** Per-binding whole-profile `ReadOnly`/`ReadWrite` over
> disjoint named bindings, on a jailed device, filesystem / Git substrate,
> same-process value-children. Enforcement is exercised on macOS Seatbelt; Linux
> Landlock is container-gated. Sub-root / `where(path=…)` grants are not part of
> this cut.

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
