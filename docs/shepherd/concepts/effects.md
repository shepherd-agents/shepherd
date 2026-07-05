# Effects

> Page status: release-ready
> Source state: shipped-source
> Applies to: Shepherd v0.2.0
> Owner: @docs-system-owner (TBD)
> Validation: scripts/check_shepherd_docs.py

*Concept. The mental model behind Shepherd. Steps live in the tutorial, signatures in the reference.*

A task's interior is opaque, you cannot step through the model's reasoning.
What you *can* see, completely, is everything that crosses the boundary. In
Shepherd every crossing is an **effect**: a named, typed value on one
explicit channel, where it can be answered, watched, refused, and recorded.

## Model delivery is an effect

The effect you meet first is the model delivery. A bodyless task performs it
for you; a bodied task can call `sp.deliver(...)` when it wants an explicit
model step. Either way, the boundary crossing is typed, recorded, and visible
on the run.

That matters because the delivery is not a string-building trick hidden inside
the decorator. It is one event in the same boundary channel as every other
external interaction Shepherd records. The model request, the model response,
and the validated return value are all evidence you can inspect when a run
does not behave the way you expected.

## Handlers answer

Effects would be inert without the receiving end. `sp.handle` installs that
receiving end for a scope: it intercepts a matching boundary event, consumes
it, and its return value becomes the answer. When handlers nest, the innermost
handler wins; outer handlers do not see an event that was already consumed.

That is how tests run without a live model. The test environment answers the
model delivery with a recorded response, and the task code stays unchanged.
Substitution happens at the boundary, not by monkey-patching the task.

## Why this buys auditability and testability

- **Auditable.** Every crossing is a typed event, and every event lands in
  the run's [trace](runs.md). "What did this program do to the world?" has a
  complete, structured answer, by construction, not by best-effort logging.
- **Testable.** Behavior at any boundary is swappable from outside, without
  touching the task: answer the model delivery, keep the task deterministic,
  and assert on the recorded boundary events. The test installs handlers; the
  code under test never knows.

## What effects are not

- **Not callbacks.** A callback is wired by a caller that knows exactly whom
  it invokes. An effect inverts that: the task states *what it needs*, and
  whoever is in scope decides how the need is met, the performer never
  names its resolver.
- **Not middleware everywhere.** There is no global pipeline every call is
  forced through. Interception is opt-in, typed, and scoped to a block,
  install nothing and effects simply meet their defaults.
- **Not log lines.** Logging describes behavior after the fact and can lie by
  omission. Effects *are* the behavior: typed, answerable, refusable, and
  recorded whether or not anyone is watching.

## Where effects sit

[Tasks](tasks.md) perform effects; the [run](runs.md) records every one; the
[workspace](workspaces.md) is the natural scope for the handlers and
model selection that meet them. The
[first Shepherd app tutorial](../tutorials/first-shepherd-app.md) shows the
task and workspace pieces in working code.
