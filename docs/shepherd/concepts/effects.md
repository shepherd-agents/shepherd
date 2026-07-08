# Effects

> Page status: release-ready
> Source state: shipped-source
> Applies to: Shepherd v0.3.0
> Owner: @docs-system-owner (TBD)
> Validation: scripts/check_shepherd_docs.py

*Concept. The mental model behind Shepherd. Steps live in the quickstart, signatures in the reference.*

A task's interior is opaque — you cannot step through an agent's reasoning.
What you *can* see, completely, is everything that crosses the boundary. In
Shepherd every crossing is an **effect**: a named, typed event on one
explicit channel, where it can be watched, refused, and recorded.

## The boundary is the record

When a retained run executes ([quickstart](../start/index.md)), everything
that crosses the boundary lands in the run's [trace](runs.md): the provider
invocation, the world operations against the bound repositories, the
retained-output capture, the settlement decision. Read it back with:

```bash
shepherd run trace --latest --events
```

"What did this program do to the world?" has a complete, structured answer,
by construction — not by best-effort logging.

## Effects are governed, not just observed

Because external work crosses one explicit channel, it is the natural place
to attach authority. The [permission grants](permissions.md) declared on a
task's signature bound what its effects may touch, and under a jailed
[placement](placements.md) that bound is enforced by the operating system —
a write outside the granted roots is refused at the syscall, and the refusal
itself is recorded.

## What effects are not

- **Not callbacks.** A callback is wired by a caller that knows exactly whom
  it invokes. An effect inverts that: the task states *what it needs*, and
  the runtime in scope decides how the need is met.
- **Not middleware everywhere.** There is no global pipeline every call is
  forced through. The boundary channel is typed and explicit; what does not
  cross it is ordinary Python.
- **Not log lines.** Logging describes behavior after the fact and can lie by
  omission. Effects *are* the behavior: typed, refusable, and recorded
  whether or not anyone is watching.

## A boundary note (0.3.0)

The 0.3.0 release ships interception machinery (`sp.handle`) for answering boundary
events in-process. The published docs deliberately do not teach it as a way
to service model calls for bodyless tasks: the ambient model-delivery lane it
would answer is a [planned surface](../roadmap.md), and answering a
task that *declares world access* with a handler that has none invites
confidently fabricated results. Test the shipped surface the way the
[quickstart](../start/index.md) does — retained runs on the deterministic
`static` provider.

## Where effects sit

[Tasks](tasks.md) perform effects; the [run](runs.md) records every one;
[permissions](permissions.md) bound them and [placements](placements.md)
decide how that bound is enforced.
