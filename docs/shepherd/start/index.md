# Getting Started

> Page status: release-ready
> Source state: checked-example
> Applies to: Shepherd v0.3.0
> Owner: @docs-system-owner (TBD)
> Validation: docs_src/quickstart/test_world_hero.py

*Quickstart. This is the path that runs on the shipped wheel, offline. For the mental model, see the concepts. For exact APIs, see the reference.*

Run one task, get its work back as a **retained output** — a proposal held
beside your files — inspect it, and settle it. Deterministic, keyless, no
network.

## Install

```bash
pip install shepherd-ai
```

Shepherd requires **Python 3.11+**. OS-level grant enforcement is executed on
both macOS (Seatbelt) and Linux (Landlock); Windows is unsupported — use WSL.
([Platform notes](../roadmap.md#platforms-030))

## Initialize a workspace

Shepherd runs inside an initialized workspace. Lead with `shepherd init`:

```bash
mkdir shepherd-demo && cd shepherd-demo
shepherd init
```

## Run

Save this as `hero.py` in that directory and run `python hero.py`:

```python
--8<-- "quickstart/world_hero.py:hero"
```

What happens, in order:

- `@sp.task` declares the task: a signature plus docstring — the contract a
  provider-run agent fulfils. The `repo: sp.GitRepo` parameter is a **writable
  workspace handle**: the signature is the permission surface, and nothing
  else authorizes the write. (Use `sp.May[sp.GitRepo, sp.ReadOnly]` when a
  task should only read.)
- `with sp.open(".") as workspace:` opens the initialized workspace and closes
  it on exit. (On an uninitialized directory it raises — run `shepherd init`
  first.)
- `workspace.tasks.register(write_note)` registers the task object directly —
  no source strings, no separate id.
- `workspace.run(write_note, repo=..., topic=..., ...)` executes it as a
  **retained run**, passing the task's own arguments as keywords.
  `provider: "static"` is the deterministic offline provider, so this run is
  reproducible and free; `"claude"` runs a live sandboxed agent instead (needs
  the `claude` CLI and auth).
- The work does **not** touch your files. It lands as a retained output; you
  read its changeset and contents first, then settle it — `select()`,
  `apply()`, `release()`, or `discard()` — exactly once.

## Output

Executed against the shipped 0.3.0 wheel (this exact transcript is what the
page's test asserts):

```text
['NOTE.txt']
Hello from a Shepherd retained output.
```

## Inspect the record

Every run leaves a durable trace you can read back from the CLI:

```bash
shepherd run list
shepherd run show --latest
shepherd run trace --latest --events
shepherd run changeset --latest
```

You can also fetch this same demo in script form with
`shepherd demo write quickstart > quickstart_demo.py`, and a live-agent
variant with `shepherd demo write agent-task` (see the
[README](https://github.com/shepherd-agents/shepherd#quickstart)).

## If it fails

- **`WorkspaceControlError` from `sp.open(".")`** — the directory is not an
  initialized Shepherd workspace. Run `shepherd init` there first.
- **Ran the script twice in the same directory?** The second run refuses at
  settlement with `InvalidRepositoryStateError` — the workspace already
  carries the first run's selected state, and re-settling an identical result
  fails closed. Start from a fresh directory when repeating the walkthrough.
- **Looking for `with sp.workspace(model=...): my_task(...)`?** That ambient
  direct-call shape is a [Dataflow roadmap surface](../roadmap.md) — there is
  no shipped model servicer, and a task that declares repository access
  refuses the ambient call outright (`AmbientWorldAccessRefused`), pointing
  you here. Retained runs, as above, are the shipped path.

## Upgrading from 0.2.x

0.3.0 modernizes the task syntax; three changes matter when upgrading:

- **One behavior removal.** An *unannotated* parameter named `repo` is no
  longer treated as a workspace handle — handles are keyed on the annotation
  only. Annotate it (`repo: sp.GitRepo`) to keep it a handle, or pass an
  ordinary value through `args={"repo": ...}` when `repo` is genuinely data.
- **`register_source(...)` is no longer the way to define a task by hand.**
  Write `@sp.task` and register the function object:
  `workspace.tasks.register(fn)`. The source-text form remains for
  machine-generated task code.
- **Task arguments pass as keywords.** `workspace.run(task, repo=h,
  topic="x")` replaces the `args={...}` dict; `args={...}` still works and is
  the escape hatch when a task parameter shares a name with a run option.

## Next

- [Grant a task repo access](../guides/grant-repo-access.md) — read-only /
  read-write grants per bound repository, enforced at the OS under a jailed
  placement.
- [Concepts: Tasks](../concepts/tasks.md) — the mental model.
- [Settlement Core / Dataflow](../roadmap.md) — what ships today vs. the named
  road.
