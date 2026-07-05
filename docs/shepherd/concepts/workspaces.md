# Workspaces

> Page status: release-ready
> Source state: shipped-source
> Applies to: Shepherd v0.2.0
> Owner: @docs-system-owner (TBD)
> Validation: scripts/check_shepherd_docs.py

*Concept. The mental model behind Shepherd. Steps live in the tutorial, signatures in the reference.*

A [task](tasks.md) deliberately says nothing about which model executes it,
which directory it works against, or which domain objects are at hand. That
silence is what keeps tasks reusable. The missing context comes from the
**workspace**, the ambient scope your tasks run inside:

```python
import shepherd as sp

with sp.workspace(model="claude:sonnet-4-5", root="./my-project"):
    review = review_change(diff)
```

Everything called inside that block, directly or nested arbitrarily deep,
sees the same model and the same root.

## Explicit, but ambient

Context handling usually forces a bad choice:

- **Thread it as parameters**, and every signature drags configuration
  through layers that never use it. On a task this is worse than clutter, it
  corrupts the signature's meaning, because task parameters are supposed to
  be *evidence for the model*, not plumbing for the framework.
- **Make it global**, and configuration becomes invisible, mutable from
  anywhere, and hostile to tests and concurrency.

The workspace is the third option: **explicit but ambient**. Ambient, because
nothing threads it by hand, any task in the block's dynamic extent can reach
it. Explicit, because it is a `with` block: you can point at the exact line
where the context begins and the exact line where it ends. Nest a second
workspace and the inner one shadows the outer for its extent; leave the block
and the outer context is restored. It is a scope, not a mutable setting.

## The scope carries model, root, and bound repositories

Model and root are the workspace's first job: the model says who answers the
tasks in scope; the root says where the program is situated. Keeping both on the
scope keeps task signatures clean: task parameters stay evidence for the model,
not framework plumbing.

The workspace also **binds repositories** — the resources a run reads and writes
under [permission grants](permissions.md). You name a bound root with
`ws.bind(root="backend/", name="backend")` (it returns a `GitRepo` value), pass
one or more to a run with `workspace.run(task, bindings={...})`, and settle each
run's retained output once with `select` / `release` / `discard`. Binding is how
the workspace hands a task exactly the world it is allowed to touch.

## The triangle: task, workspace, run

Three nouns are easy to blur and worth keeping sharp:

- A **[task](tasks.md)** is the *declaration*: what should happen, typed.
  Timeless and context-free.
- A **workspace** is the *situation*: which model, where, with what at
  hand. It spans many calls.
- A **[run](runs.md)** is the *event*: one execution, fully recorded.

Call a task inside a workspace and you get a run. Same task, two different
workspaces: two runs you can [compare](runs.md). Same workspace, many tasks:
one consistent situation. Each noun answers a different question, *what*,
*where and with what*, and *what happened*.

## What a workspace is not

- **Not a global config singleton.** It is scoped, nestable, and shadowable;
  two workspaces can coexist in one program without ever seeing each other.
- **Not a conversation.** A workspace accumulates no model memory between
  calls. Tasks inside it remain independent invocations that happen to share
  configuration, context here is *configuration*, not *history*.

## Where workspaces sit

The [first Shepherd app tutorial](../tutorials/first-shepherd-app.md) opens
its workspace in the first ten lines. The model it binds comes from a
[provider](providers.md). [Effect](effects.md) handlers commonly install at
workspace scope, and every [run](runs.md) carries the context it executed
under as part of its record.
