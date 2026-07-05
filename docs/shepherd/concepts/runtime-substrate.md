# Runtime substrate

> Page status: scaffold
> Source state: preview
> Applies to: Shepherd v0.1.1-dev
> Owner: @docs-system-owner (TBD)
> Validation: scripts/check_shepherd_docs.py

*Concept. The mental model behind Shepherd. Steps live in the tutorial, signatures in the reference.*

!!! warning "Partly shipped"
    Part of this substrate model has shipped: `GitRepo` handles, per-binding
    grants, and the **`Changeset`** that reifies what a run changed are on the
    public `shepherd` surface in 0.2.0 (see [Permissions](permissions.md)). The
    unified, signature-level handle-in/**handle-out** API shown below — output
    handles threaded forward, and substrates beyond the filesystem / Git repo —
    is still the target shape, not yet the importable one.

A **substrate** is a kind of world a task can act on — in 0.2.0, the filesystem
and a Git repo. The runtime substrate model is about making a task's
relationship to that world **explicit on both ends**: which world it operates
on, and what it changed.

Today both ends are erased. A task's effect on the world reaches you implicitly,
it mutates an ambient workspace, and to learn *what* it changed you
reconstruct the answer from the after-the-fact trace. The honest type of such a
task hides two things its signature never says: the world it read, and the world
it produced.

## Handles make the world a value

The substrate model hands a task a **handle**: a typed, value-shaped view of one
bound substrate — for example a `GitRepo` — carrying the substrate's own
methods. A handle is taken at a known **basis**: a
content-addressed identity of its input state, the precise "the world as of
*here*" the task started from. A handle is a value, not a connection or a lock,
two views at the same basis with the same authority are interchangeable.

```python
@shp.task
def evaluate_fix(repo: May[GitRepo, ReadWrite], diff: str) -> tuple[GitRepo, Verdict]:
    """Apply the diff on a branch, run the checks, and report a verdict."""
```

`repo` names the world this task operates on, right in the signature. The task
no longer reaches for an ambient "current repo"; it is handed one, at a basis it
can name.

## What changed comes back as a value

If the world-input is a handle, the world-*output* is a returned value too. A
write-like step on a `GitRepo@A` yields a new `GitRepo@B`, a handle denoting
the resulting state. The caller **threads it forward** to keep going from `B`,
or **ignores it** and stays at `A`; that is ordinary dataflow, not a special
"commit" verb.

Alongside it, every run carries a **`Changeset`**: the world-output reified as a
typed, inspectable bundle of what changed, per substrate. Crucially, a changeset
is always a *view* over the run's recorded trace, never a second store you have
to keep in sync. "What did that task change?" stops being a reconstruction job
and becomes a value you read.

So the honest shape of a task becomes explicit at both ends: ordinary arguments
and substrate handles go in; ordinary values, output handles, and a changeset
come out.

## What the substrate is *not*

- **Not the workspace.** The [workspace](workspaces.md) is the ambient
  *configuration*, model, root, shared context, and is identity-shaped. A
  handle is a *value-shaped view of one bound substrate*; you can hold several,
  compare them, and pass them around.
- **Not a database or a parallel store.** A `Changeset` is a view computed over
  the recorded trace. It adds no new place where state lives and nothing to
  reconcile against the trace.
- **Not implicit mutation.** A task does not quietly change an ambient world.
  The world it may touch is named in its signature, and what it changed is
  returned, visible, typed, and ignorable.

## Where the substrate sits

The runtime substrate is the *world* the other concepts act on. A
[task's](tasks.md) signature names the substrate it operates on; the
[permissions](permissions.md) on that handle bound what it may do to it; the
[effects](effects.md) are the individual operations that cross the boundary;
and the [run's](runs.md) trace is what the changeset is a view over. Handles
make the world an input and an output like any other.
