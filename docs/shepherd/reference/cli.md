# CLI

> Page status: release-ready
> Source state: shipped-source
> Applies to: Shepherd v0.3.0
> Owner: @docs-system-owner (TBD)
> Validation: scripts/gen_cli_reference.py --check

*Reference. Exact, generated facts. The mental model lives in concepts, recipes in guides.*

The `shepherd` command (also installed as `sp`) ships in 0.3.0. The help blocks
below are captured verbatim from the shipped CLI. Read-only listings accept
`--json` for a durable machine payload.

**Read vs. settle — the identity rule.** Read commands (`show`, `changeset`,
`trace`, …) accept selectors: `--latest` and a unique short run-id prefix.
Settlement commands (`select` / `apply` / `release` / `discard`) require an
*exact* run identity and reject selectors. Settlement is consume-once: after one
records its outcome, the others refuse for that output.

`run start` is a fenced compatibility entry point, not the normal launch path —
it fails closed unless `SHEPHERD_ENABLE_FENCED_RUN_START=1` is set. The sanctioned
Python launch is `workspace.run(...)`.

## `shepherd`

```text
Usage: shepherd [OPTIONS] COMMAND [ARGS]...

  Shepherd — effect-based AI agent framework.

Options:
  --version  Show the version and exit.
  --help     Show this message and exit.

Commands:
  demo     Emit checked-in quickstart demo scripts.
  doctor   Check whether the current directory is ready for the quickstart.
  init     Initialize PATH as a Shepherd workspace.
  package  Create and manage Shepherd extension packages.
  run      Inspect runs and settle retained outputs; start is a fenced...
  task     Manage and inspect task-library entries.
```

## `shepherd run`

```text
Usage: shepherd run [OPTIONS] COMMAND [ARGS]...

  Inspect runs and settle retained outputs; start is a fenced compatibility
  entry point.

Options:
  --help  Show this message and exit.

Commands:
  apply                           Apply one retained run output onto...
  changeset                       Inspect the read-only changeset view...
  discard                         Discard one retained run output as...
  list                            List run summaries from the selected...
  output-citations                List raw run-ledger output citations...
  outputs                         List product run outputs after...
  publish-retained-workspace-output
                                  Publish or repair the retained...
  release                         Release one retained run output...
  repair                          Reclaim orphaned operation refs left...
  select                          Select one retained run output into...
  show                            Show one run record from the...
  start                           Run the fenced compatibility start...
  trace                           Print the materialized trace...
  trace-revision                  Print the run-trace summary for one...
  vcscore                         Show the vcs-core citations carried...
```

## `shepherd task`

```text
Usage: shepherd task [OPTIONS] COMMAND [ARGS]...

  Manage and inspect task-library entries.

Options:
  --help  Show this message and exit.

Commands:
  list      List task summaries from the selected task ledger.
  register  Register a task import path as an active task version.
  resolve   Resolve a task ref to an exact artifact lock.
  show      Show one task definition and its signature/permission surface.
```

## `shepherd demo`

```text
Usage: shepherd demo [OPTIONS] COMMAND [ARGS]...

  Emit checked-in quickstart demo scripts.

Options:
  --help  Show this message and exit.

Commands:
  write  Write demo NAME to standard output.
```

## `shepherd doctor`

```text
Usage: shepherd doctor [OPTIONS] [COMMAND] [ARGS]...

  Check whether the current directory is ready for the quickstart.

Options:
  --json                          Emit machine-readable readiness JSON.
  --backend [auto|clonefile|fuse|kernel|copy]
                                  Workspace backend to validate.  [default:
                                  auto]
  --help                          Show this message and exit.

Commands:
  claude  Check whether the live Claude runtime lane is available.
```

## `shepherd init`

```text
Usage: shepherd init [OPTIONS] [PATH]

  Initialize PATH as a Shepherd workspace.

  This creates or reuses ``.vcscore`` in PATH, validates the workspace-control
  substrate, and leaves ordinary Git history untouched. Use ``sp package init
  NAME`` for package scaffolding.

Options:
  --backend [auto|clonefile|fuse|kernel|copy]
                                  Filesystem carrier backend to validate with.
                                  [default: auto]
  --adopt [none|git-head|worktree]
                                  Record an existing project baseline into
                                  Shepherd custody.  [default: worktree]
  --init-git / --no-init-git      Initialize a Git repository when PATH is not
                                  already inside one.  [default: init-git]
  --help                          Show this message and exit.
```

## `shepherd package`

```text
Usage: shepherd package [OPTIONS] COMMAND [ARGS]...

  Create and manage Shepherd extension packages.

Options:
  --help  Show this message and exit.

Commands:
  init  Create a new Shepherd package.
```
