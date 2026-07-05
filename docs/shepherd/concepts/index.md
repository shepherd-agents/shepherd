# Concepts

> Page status: release-ready
> Source state: shipped-source
> Applies to: Shepherd v0.2.0
> Owner: @docs-system-owner (TBD)
> Validation: scripts/check_shepherd_docs.py

*Concept. The mental model behind Shepherd. Steps live in the tutorial, signatures in the reference.*

This section is the mental-model layer of the Shepherd docs. Four ideas carry
the whole framework; each gets one page, and the pages keep linking to each
other because the ideas genuinely interlock. Steps live in the
[tutorial](../tutorials/first-shepherd-app.md), exact signatures live in the
reference, *why the framework is shaped this way* lives here.

## The four ideas

| Page | The idea in one line |
| --- | --- |
| **[Tasks](tasks.md)** | A task is a typed function whose body the model fills in; the signature is the contract. |
| **[Effects](effects.md)** | Everything a task does to the world crosses one explicit, typed, interceptable channel. |
| **[Runs](runs.md)** | Every execution leaves a durable record; debugging is reading that record, not guessing. |
| **[Workspaces](workspaces.md)** | Context, model, root, shared objects, is ambient but explicit: a scope, not a global. |

How they interlock: a **task** declares what should happen; a **workspace**
supplies the situation it happens in; calling the task produces a **run**; and
the run's trace is populated by the **effects** that crossed the boundary
along the way. Pull any one of the four out and the other three stop making
sense, which is why the reading order below matters less than it looks.

Beyond the four pillars, **[Providers](providers.md)** goes deeper on the
model backend a workspace binds.

## If you came here to build

You do not need this section to ship your first feature, the
[first Shepherd app tutorial](../tutorials/first-shepherd-app.md) gets you to
working code without it. Come back when something surprises you, and enter
through the question that brought you:

- "Why did editing a *docstring* change behavior?" → [Tasks](tasks.md)
- "Who answered that request, and who else saw it?" → [Effects](effects.md)
- "What did that call actually *do*?" → [Runs](runs.md)
- "Where did the model and that binding come from?" → [Workspaces](workspaces.md)

Each page is written to stand alone; cross-links fill whatever gaps remain.

## If you came here to evaluate

Read the four pages in order, [tasks](tasks.md), [effects](effects.md),
[runs](runs.md), [workspaces](workspaces.md). They build outward from the
unit of work to its channel, its record, and its context.

These concepts build outward from the surface these docs exercise:
tasks, effects, runs, workspaces, and provider selection, plus the
[per-binding signature grants](permissions.md) enforced at the syscall jail
and the [placements](placements.md) that decide where they are enforced —
both shipped in 0.2.0. Workflow packaging catalogs and live-provider
operations are covered once their product surfaces land.
