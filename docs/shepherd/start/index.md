# Getting Started

> Page status: release-ready
> Source state: checked-example
> Applies to: Shepherd v0.2.0
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

Shepherd requires **Python 3.11+**. OS-level grant enforcement is exercised on
macOS (Seatbelt); on Linux, Landlock enforcement is container-gated today;
Windows is unsupported — use WSL. ([Platform notes](../roadmap.md#platforms-020))

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

- `sp.open(".")` opens the initialized workspace. (On an uninitialized
  directory it raises — run `shepherd init` first.)
- `register_source` registers a task. The task is a signature plus docstring —
  the contract a provider-run agent fulfils. The grant on `repo`
  (`May[GitRepo, ReadWrite]`) is what would let it write the repository; the
  signature is the permission surface.
- `workspace.run(...)` executes it as a **retained run**. `provider: "static"`
  is the deterministic offline provider, so this run is reproducible and free;
  `"claude"` runs a live sandboxed agent instead (needs the `claude` CLI and
  auth).
- The work does **not** touch your files. It lands as a retained output; you
  read its changeset and contents first, then settle it — `select()`,
  `release()`, or `discard()` — exactly once.

## Output

Executed against the shipped 0.2.0 wheel (this exact transcript is what the
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
- **Ran the script twice in the same directory?** The second run raises
  `InvalidRepositoryStateError` (`readiness blocked by run-...`) — the
  workspace still holds the first run's registration. Start from a fresh
  directory when repeating the walkthrough.
- **Looking for `with sp.workspace(model=...): my_task(...)`?** That ambient
  direct-call shape is a [Dataflow roadmap surface](../roadmap.md) — it does
  not run on the 0.2.0 wheel. Retained runs, as above, are the shipped path.

## Next

- [Grant a task repo access](../guides/grant-repo-access.md) — read-only /
  read-write grants per bound repository, enforced at the OS under a jailed
  placement.
- [Concepts: Tasks](../concepts/tasks.md) — the mental model.
- [Settlement Core / Dataflow](../roadmap.md) — what ships today vs. the named
  road.
