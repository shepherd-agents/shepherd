# Permissions

> Page status: release-ready
> Source state: shipped-source
> Applies to: Shepherd v0.3.0
> Owner: @docs-system-owner (TBD)
> Validation: scripts/check_shepherd_docs.py

*Concept. The mental model behind Shepherd. Steps live in the quickstart, signatures in the reference.*

A task's **permissions are part of its signature**. Just as the return type
declares what you get back, a per-parameter grant declares what the task may do
to each resource it is handed. Reading the signature *is* reading the permission
surface; there is no second, hidden policy file that the code merely
approximates.

A task declares a read-only or read-write grant **per bound repository**, right
in its signature. There are two spellings of the same grant, one ladder:

```python
from shepherd import task, May, GitRepo, ReadOnly, ReadWrite

@task
def write_note(repo: GitRepo, topic: str) -> None: ...
    # bare GitRepo — the writable workspace handle, the beginner spelling

@task
def apply_documented_fix(
    docs:    May[GitRepo, ReadOnly],   # read-only: writes refused at the OS
    backend: May[GitRepo, ReadWrite],  # writable root, spelled explicitly
    issue:   str,
) -> None: ...
```

A bare `GitRepo` parameter **is** a writable grant — equivalent to
`May[GitRepo, ReadWrite]`, keyed on the annotation, never on the parameter's
name. Reach for `May[...]` the moment a task should hold less than full write:
`docs` may be read but not written; `backend` is a writable root. Either way
the grant rides the parameter, so the security surface and the program are the
same artifact — there is no separate policy document to drift out of sync.
(Registrations record which spelling was written, so defaulted-writable grants
stay countable.)

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
**consume-once** — with `select`, `apply`, `release`, or `discard`:

```python
run.changeset(name="backend")   # what the task wrote to the backend binding
```

Nothing the task wrote touches your working files — the delta stays a
retained output, read through the changeset surface; settlement records
your decision on it exactly once.

## The whole-run floor: `may=`

Below the per-parameter grants sits a whole-run ceiling that still works from
v0.1: pass `may=` to a run to cap everything it may do.

```python
run = workspace.run(task, repo=workspace.git_repo(), may="ReadOnly")
```

`may="ReadOnly"` makes the entire run read-only; like the per-parameter grants,
it is compiled into the jail and enforced at the syscall, not merely advised.
A task's registered ceiling is **derived from its signature's grants** — you
don't declare it twice; an explicit `may_default=` at registration still
overrides when you need a different floor.

!!! note "Scope (0.3.0)"
    Per-binding whole-profile `ReadOnly`/`ReadWrite` over disjoint named
    bindings, under jailed placement, filesystem / Git substrate, same-process
    value-children. Enforcement is executed on both macOS Seatbelt and Linux
    Landlock. Sub-root / `where(path=…)` grants are not part of this cut.

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
[runtime substrate](runtime-substrate.md), handles that carry their own
authority. Permissions are the rules; effects are the traffic; the substrate is
the world being governed.
