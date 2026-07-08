# Settlement Core / Dataflow

> Page status: release-ready
> Source state: shipped-source
> Applies to: Shepherd v0.3.0
> Owner: @docs-system-owner (TBD)
> Validation: scripts/check_shepherd_docs.py

This page is the honest map of Shepherd today. It divides the product into two
named halves so you always know which one you are reading about:

- **Settlement Core** — what ships in `shepherd-ai` 0.3.0. Everything in this
  half runs on the installed wheel, today, and is what the rest of the
  published docs teach.
- **Dataflow** — the named road. The surfaces that make agent results flow
  like ordinary values — returned handles, typed value projection, task-to-task
  delegation — are designed and sequenced, but **not shipped**. Nothing in this
  half runs on 0.3.0.

If a page you saw referenced is missing from this site, it taught a Dataflow
idiom ahead of the wheel and was pulled until the surface it teaches actually
ships. This page is its forwarding address.

## Settlement Core — ships in 0.3.0

The shipped product is a **settlement machine**: agent work is captured to one
side, reviewed as data, and settled — kept or rejected — explicitly, exactly
once. Concretely:

- **Retained runs** *(shipped)*. `workspace.run(...)` executes a task and holds
  its world output as a **retained output** — a proposal to one side of your
  files. Nothing touches your working tree until you settle it.
  See [Runs](concepts/runs.md).
- **Signature grants over named bindings** *(shipped)*. Permissions are part of
  the task's signature: a bare `repo: GitRepo` parameter is the writable
  workspace handle, and `May[GitRepo, ReadOnly]` / `May[GitRepo, ReadWrite]`
  are the explicit spellings — per bound repository, over disjoint named
  bindings. Under jailed placement the grant is enforced at the native syscall
  jail. See [Permissions](concepts/permissions.md) and
  [Placements](concepts/placements.md).
- **Per-binding changesets** *(shipped)*. Each binding's world output is
  inspectable on its own: `run.changeset(name="backend")` is a read-only view
  of exactly what the run wrote where.
  See [Grant a task repo access](guides/grant-repo-access.md).
- **Explicit settlement** *(shipped)*. Every retained output is settled
  **once**, explicitly, with `select`, `apply`, `release`, or `discard` —
  consume-once, recorded, and refused on re-settlement. Where `select` is
  fast-forward-only (it fails closed if the workspace moved on since the run's
  fork basis), **`apply`** three-way-settles a kept output onto the advanced
  workspace when the two change sets are path-disjoint — whole-output,
  path-disjoint or refused, never content synthesis. You read retained content
  through the changeset surface (`shepherd run changeset --latest --read
  <path>`) before deciding. *Known limitation:* an in-session `select` →
  re-acquire → run loop can misreport the next run's changeset and
  phantom-refuse `apply`; fork all candidates before your first settlement, or
  use one settlement chain per workspace session (fix in progress).
- **The recorded trace** *(shipped)*. Every run leaves a durable record;
  `shepherd run trace <run-ref>` reads it back. Debugging is reading the
  record, not guessing.

The [Getting Started](start/index.md) quickstart exercises this whole loop —
initialize, run, inspect the changeset, settle — against the shipped wheel,
offline and deterministically.

## Dataflow — the named road (not in 0.3.0)

These are the surfaces that make Shepherd programs compose like ordinary
Python. They are named here so that hitting one reads as "not yet", never as
"broken":

- **Ambient model service for direct task calls** *(roadmap — not in 0.3.0)*.
  The elegant shape `with sp.workspace(model=...): my_task(...)` — a bodyless
  task answered directly by a model — has no shipped servicer. On the 0.3.0
  wheel a pure task's ambient call fails loudly at delivery (`DeliveryFailed`:
  no handler installed), and a bodyless task that **declares repository
  access** refuses before launch (`AmbientWorldAccessRefused`), naming the
  working path: run it through retained execution, `workspace.run(...)`.
- **Returned handles** *(roadmap)*. Handles flow **in** today — `repo: GitRepo`
  in a signature is a shipped grant — but tasks whose *return types* carry
  world resources (for example `-> GitRepo`, or `-> tuple[GitRepo, Report]`)
  are a Dataflow surface, and 0.3.0 **refuses** such return slots rather than
  letting a model fabricate a handle value. A task's world output arrives as a
  retained changeset, not as a returned handle value.
- **Typed value projection from captured work** *(roadmap)*. Deriving a typed
  return value — part proof-from-capture, part model-reported testimony,
  clearly labeled which is which — from a retained run's changeset is designed
  but not shipped.
- **Threading and durable children** *(roadmap)*. Passing retained results
  between tasks and supervising long-lived child runs as first-class values.
- **Task-as-value delegation** *(roadmap — explicitly deferred)*. The
  meta-agent shape where one task takes another task as an argument and
  supervises it — `oversee(implement, ...)`, retry-until-acceptable — is the
  product's north star and is **deferred**: no shipped 0.3.0 surface runs it.
  Its honest form today is plain Python around retained runs: run, inspect the
  changeset, keep or discard, retry.

When we are unsure which half a surface belongs to, it goes here — Dataflow —
until an executed test against the shipped wheel says otherwise.

## Platforms (0.3.0)

Shepherd requires **Python 3.11+**. OS-level grant enforcement is executed on
**both macOS** (Seatbelt) and **Linux** (Landlock, exercised in a privileged
container in CI). **Windows is unsupported** — grants would be advisory-only at
best; use **WSL**.

## Reading claims on this site

Every published page carries an "Applies to" version and teaches only what
runs on that shipped wheel, or labels the exception explicitly (simulated or
illustrative output is marked as such). If you find a published sentence that
does not run on the wheel, that is a bug in the docs — please
[report it](https://github.com/shepherd-agents/shepherd/issues).
