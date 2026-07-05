# Permissions

> Page status: release-ready
> Source state: shipped-source
> Applies to: Shepherd v0.2.0
> Owner: @docs-system-owner (TBD)
> Validation: shepherd/packages/dialect/tests/test_lane_c_acceptance_gate.py

*Concept. The mental model behind Shepherd. Steps live in the tutorial, signatures in the reference.*

A task's **permissions are part of its signature**. Just as the return type
declares what you get back, a per-parameter grant declares what the task may do
to each resource it is handed. Reading the signature *is* reading the permission
surface; there is no second, hidden policy file that the code merely
approximates.

A task declares a read-only or read-write grant **per bound repository**, right
in its signature:

```python
from shepherd import task, May, GitRepo, ReadOnly, ReadWrite

@task
def apply_documented_fix(
    docs:    May[GitRepo, ReadOnly],   # read-only: writes refused at the OS
    backend: May[GitRepo, ReadWrite],  # writable root
    issue:   str,
) -> None: ...
```

`docs` may be read but not written; `backend` is a writable root. The grant
rides the parameter, so the security surface and the program are the same
artifact — there is no separate policy document to drift out of sync.

## The grant lowers to the syscall jail

Under jailed placement (`placement="jail"`, or `"auto"` on a jail-capable host) the grant is compiled to that run's writable roots and
**enforced at the native syscall jail** (macOS Seatbelt; Linux Landlock). A
write to a `ReadOnly`-granted repository, or to any managed path not covered by
a `ReadWrite` grant, is refused at the syscall — before the last undo point, not
merely advised and not caught only at a merge gate. Authority defaults to deny:
a repository the signature never grants write to is read-only to the task.

Because the jail is the enforcement point, permissions do not depend on caller
discipline. A careful caller and a careless one get the same enforced surface:
the writable roots are a property of the run's grants, checked by the OS, not a
convention the caller is trusted to honor.

`shepherd task show <name>` renders the grant surface expanded, so you can read
exactly what a task may touch before you run it.

## Grants are whole-profile per binding

A grant applies to a **whole bound repository**: a bound repository is entirely
writable or entirely read-only. Named bindings are **disjoint** — their roots do
not overlap — so every managed path belongs to exactly one binding and is
governed by that binding's single grant.

Bindings are named when you bind a root:

```python
backend = ws.bind(root="backend/", name="backend")
```

and passed to a run by name:

```python
run = workspace.run(task, bindings={"docs": docs, "backend": backend})
```

A run with a single binding stays `repo=`:

```python
run = workspace.run(task, repo=workspace.git_repo())
```

Each binding's world output is inspected on its own, and settled once —
**consume-once** — with `select`, `release`, or `discard`:

```python
run.changeset(name="backend")   # what the task wrote to the backend binding
```

Nothing the task wrote touches your files until you `select` it.

## The whole-run floor: `may=`

Below the per-parameter grants sits a whole-run ceiling that still works from
v0.1: pass `may=` to a run to cap everything it may do.

```python
run = workspace.run(task, repo=workspace.git_repo(), may="ReadOnly")
```

`may="ReadOnly"` makes the entire run read-only; like the per-parameter grants,
it is compiled into the jail and enforced at the syscall, not merely advised. A
task registered with `may_default=` sets that same floor at registration time.

!!! note "Scope (0.2.0)"
    Per-binding whole-profile `ReadOnly`/`ReadWrite` over disjoint named
    bindings, under jailed placement, filesystem / Git substrate, same-process
    value-children. Enforcement is exercised on macOS Seatbelt; Linux Landlock
    is container-gated. Sub-root / `where(path=…)` grants are not part of this
    cut.

## What permissions are *not*

- **Not runtime trust.** A task does not ask, at runtime, whether it is allowed
  to write and hope the answer is yes. The writable roots are fixed by the run's
  grants and bound the body at the syscall no matter how it tries to act.
- **Not capabilities discovered in production.** Authority is declared up front
  and defaults to deny: a repository the signature never grants write to is
  read-only, not silently writable.
- **Not a sandbox bolted on beside the code.** The grant lives on the parameter,
  so the security surface and the program are the same artifact. There is no
  separate policy document to drift out of sync.

## Where permissions sit

Permissions are the authority half of a [task's](tasks.md) contract: the
signature says both *what it computes* (parameters and return type) and *what it
may touch*. What actually crosses the boundary at runtime are
[effects](effects.md), and the resources they act on are the
[runtime substrate](index.md), handles that carry their own
authority. Permissions are the rules; effects are the traffic; the substrate is
the world being governed.
