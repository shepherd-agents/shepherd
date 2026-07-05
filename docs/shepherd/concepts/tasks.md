# Tasks

> Page status: release-ready
> Source state: shipped-source
> Applies to: Shepherd v0.2.0
> Owner: @docs-system-owner (TBD)
> Validation: scripts/check_shepherd_docs.py

*Concept. The mental model behind Shepherd. Steps live in the tutorial, signatures in the reference.*

A **task** is a typed Python function whose body the model fills in. You
write the signature, the types, and a docstring that states the goal;
Shepherd turns that contract into a model invocation and hands you back a
value of the declared return type, or a typed failure.

```python
import shepherd as sp

@sp.task
def review_for_security(diff: str, project: str) -> SecurityReview:
    """Identify security concerns in this code change."""
```

That function has no body, and that is the point: the contract *is* the
program. What the model is asked, and what you get back, are both projections
of those few lines.

## The signature carries the meaning

Every part of a task's declaration does semantic work; nothing is decoration.

- **Parameters are evidence.** The arguments you pass are what the model is
  shown, the diff under review, the project name. A parameter is not
  plumbing; it is the material the model reasons over, presented under the
  name and type you gave it.
- **The return type is the contract.** `-> SecurityReview` is not a hint. The
  model's response is validated against it, and the caller receives a real
  `SecurityReview` value, or a typed delivery failure. Never a maybe-shaped
  blob of text to parse by hand.
- **The docstring is the instruction.** It states the goal in plain English,
  and it is rendered to the model as the thing to do, not kept around for
  human readers only.

One declaration is read by two audiences: your type checker and the model,
and means the same thing to both. That is the core trick: there is no second,
hidden prompt that the signature merely approximates.

## The docstring is not a comment

In ordinary Python a docstring is inert documentation. On a task it is
behavioral: edit the docstring and you have edited the program, exactly as if
you had edited a function body in classical code. Two tasks identical except
for their docstrings are two different programs sharing a signature.

Treat docstring edits accordingly, review them as behavior changes, not
style fixes. "Tightened the wording" on a task docstring is the same kind of
change as "tightened the loop condition" anywhere else.

## Bodyless and bodied tasks

The bodyless form above is the purest shape: signature plus docstring, one
synthesized model delivery. When a task needs to orchestrate, call the model
more than once, combine intermediate results, invoke other tasks, give it a
body, where `sp.deliver` is the explicit "go to the model now" step:

```python
@sp.task
def audit_change(diff: str) -> str:
    """Audit a code change; return a one-paragraph summary."""
    full = sp.deliver(str, goal="Produce a detailed audit.")
    return sp.deliver(str, goal="Summarize the audit.", evidence=[full])
```

A bodied task is ordinary Python in the middle with model deliveries at the
edges. The body decides *when* and *with what evidence* the model is invoked;
each delivery flows through the same contract machinery as the bodyless form.

## What a task is not

- **Not a prompt template.** A template produces a string and walks away. A
  task carries types in and out, validates what returns, records what
  happened (see *Runs*), and declares what it may touch. The prompt
  is a *derived artifact* of the contract, not the thing you author.
- **Not a raw model call.** Calling a provider directly gets you text and a
  shrug. Calling a task gets you a validated value, a typed failure mode, and
  a durable record, the difference between an HTTP request and a database
  transaction.
- **Not an implicit side-effecting agent.** External work crosses explicit
  model and effect boundaries, so the run can record what happened instead of
  leaving you to infer it from logs.

## Where tasks sit

A task is the *declaration*. Calling it inside a *workspace*, which supplies
the model and the working context, produces a *run*, the durable record of
that one execution; anything it says to the world beyond the model travels
through *effects*. To see all four moving together, start with the
[first Shepherd app tutorial](../tutorials/first-shepherd-app.md).
