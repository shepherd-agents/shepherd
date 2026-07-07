# Tasks

> Page status: release-ready
> Source state: shipped-source
> Applies to: Shepherd v0.2.0
> Owner: @docs-system-owner (TBD)
> Validation: scripts/check_shepherd_docs.py

*Concept. The mental model behind Shepherd. Steps live in the quickstart, signatures in the reference.*

A **task** is a typed Python function used as a **contract**. The signature
declares what the task is given and what it may touch; the docstring states
the goal. In shipped 0.2.0, the way that contract is executed is a
**retained run**: a provider-run agent acts as the task's body, and its work
comes back as a retained output you inspect and settle
(see [Runs](runs.md)).

```python
import shepherd as sp

@sp.task
def write_note(repo: sp.May[sp.GitRepo, sp.ReadWrite], topic: str) -> None:
    """Write one note about `topic` into the repository."""
```

That function has no body, and that is the point: the contract *is* the
program. What the agent is asked to do, and what it is allowed to touch, are
both projections of those few lines.

## The signature carries the meaning

Every part of a task's declaration does semantic work; nothing is decoration.

- **Parameters are the inputs.** The arguments you pass are the material the
  work is done over — the repository, the topic — presented under the name
  and type you gave them.
- **Grants ride the parameters.** `May[GitRepo, ReadWrite]` on `repo` is a
  [permission](permissions.md) declaration: reading the signature *is*
  reading the permission surface, and under a jailed
  [placement](placements.md) the grant is enforced by the operating system.
- **The docstring is the instruction.** It states the goal in plain English,
  and it is what the executing agent is actually asked to do — not
  documentation kept around for human readers only.

## The docstring is not a comment

In ordinary Python a docstring is inert documentation. On a task it is
behavioral: edit the docstring and you have edited the program, exactly as if
you had edited a function body in classical code. Two tasks identical except
for their docstrings are two different programs sharing a signature.

Treat docstring edits accordingly — review them as behavior changes, not
style fixes. "Tightened the wording" on a task docstring is the same kind of
change as "tightened the loop condition" anywhere else.

## Bodyless and bodied tasks

The bodyless form above is the purest shape: signature plus docstring, the
whole body delegated to the executing agent of a retained run
(`workspace.run(...)` — the [quickstart](../start/index.md) runs one
end-to-end). A task can instead have a **body** — ordinary Python you wrote —
which runs as ordinary Python.

One boundary to be explicit about, because it is easy to assume otherwise:
**calling a bodyless task directly** — `my_task(...)` inside
`with sp.workspace(model=...)` — is a Dataflow surface that has **not**
shipped: on the 0.2.0 wheel there is no ambient model servicer, and the call
fails loudly rather than reaching a model. The shipped execution path for a
bodyless task is the retained run. See
[Settlement Core / Dataflow](../roadmap.md).

## What a task is not

- **Not a prompt template.** A template produces a string and walks away. A
  task carries typed inputs, declares what it may touch, and its execution
  leaves a durable record (see [Runs](runs.md)). The prompt is a *derived
  artifact* of the contract, not the thing you author.
- **Not a raw model call.** Calling a provider directly gets you text and a
  shrug. Running a task gets you a recorded run, a reviewable retained
  output, and an explicit settlement step — the difference between an HTTP
  request and a database transaction.
- **Not an implicit side-effecting agent.** A task's work is captured to a
  retained output beside your files; nothing lands silently. What crossed the
  boundary is recorded on the run as [effects](effects.md).

## Where tasks sit

A task is the *declaration*. Executing it as a retained run produces a
[run](runs.md), the durable record of that one execution; its declared
[permissions](permissions.md) bound what the execution may touch, and the
[placement](placements.md) decides whether the OS enforces that bound. The
[quickstart](../start/index.md) shows all of it in one working program.
