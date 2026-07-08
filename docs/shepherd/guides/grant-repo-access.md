# Grant a task write access to a repository

> Page status: release-ready
> Source state: shipped-source
> Applies to: Shepherd v0.3.0
> Owner: @docs-system-owner (TBD)
> Validation: shepherd/packages/dialect/tests/test_lane_c_acceptance_gate.py

*How-to guide. New to Shepherd? Start with the quickstart. For exact APIs, see the reference.*

**Job.** Give a task read-write access to one bound repository and read-only
access to another, so a violation is refused by the operating system — not
caught by convention. This is the per-binding signature-grant surface
(the mental model, including the bare `repo: GitRepo` writable shorthand, is
in [Permissions](../concepts/permissions.md)).

## 1. Declare the grants in the signature

Each parameter carries a grant. `docs` may be read but not written; `backend`
is a writable root:

```python
from shepherd import task, May, GitRepo, ReadOnly, ReadWrite

@task
def apply_documented_fix(
    docs:    May[GitRepo, ReadOnly],   # read-only: writes refused at the OS
    backend: May[GitRepo, ReadWrite],  # writable root
    issue:   str,
) -> None: ...
```

Reading the signature *is* reading the permission surface. `shepherd task show
apply_documented_fix` renders it expanded (`docs read-only / backend read-write`).

## 2. Bind the repositories by name

Bound roots must be **disjoint** — Shepherd refuses overlapping or nested binds
at bind time, so every managed path belongs to exactly one binding:

```python
docs    = ws.bind(root="docs/",    name="docs")     # returns a GitRepo value
backend = ws.bind(root="backend/", name="backend")
```

## 3. Run on a jailed placement

Pass the bound repositories by name, and choose a jailed [placement](../concepts/placements.md)
so the grants are enforced by the OS rather than merely recorded:

```python
run = ws.run(
    apply_documented_fix,
    bindings={"docs": docs, "backend": backend},
    issue="#142",
    placement="jail",          # writable roots compiled from the grants; Seatbelt/Landlock
)
```

A write to `docs/` — or to any managed path not covered by a `ReadWrite` grant —
is refused at the syscall, before the last undo point. On a jail-less host,
`placement="jail"` fails closed rather than downgrading silently.

## 4. Inspect and settle

Review what the run produced per binding, then settle its retained output once:

```python
cs = run.changeset(name="backend")   # a read-only view of one binding's delta
print(cs.changed_paths)
ws.select(run.output())              # keep it; or ws.release(...) / ws.discard(...)
```

Settlement is **consume-once**: after one of `select` / `apply` / `release` /
`discard` records its outcome, the others refuse for that output.

!!! note "Scope (0.3.0)"
    Grants are whole-profile per binding (a bound repository is entirely
    writable or entirely read-only). Enforcement is executed on both macOS
    Seatbelt and Linux Landlock. Sub-root / `where(path=…)` grants are not
    part of this cut.
