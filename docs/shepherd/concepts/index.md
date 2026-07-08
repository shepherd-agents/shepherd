# Concepts

> Page status: release-ready
> Source state: shipped-source
> Applies to: Shepherd v0.3.0
> Owner: @docs-system-owner (TBD)
> Validation: scripts/check_shepherd_docs.py

*Concept. The mental model behind Shepherd. Steps live in the quickstart, signatures in the reference.*

This section is the mental-model layer of the Shepherd docs. Five ideas carry
the shipped framework; each gets one page, and the pages keep linking to each
other because the ideas genuinely interlock. Steps live in the
[quickstart](../start/index.md), exact signatures live in the reference,
*why the framework is shaped this way* lives here.

## The five ideas

| Page | The idea in one line |
| --- | --- |
| **[Tasks](tasks.md)** | A task is a typed function used as a contract; the signature carries the meaning, including its permissions. |
| **[Effects](effects.md)** | Everything a task does to the world crosses one explicit, typed, recorded channel. |
| **[Runs](runs.md)** | Every execution leaves a durable record; debugging is reading that record, not guessing. |
| **[Permissions](permissions.md)** | Per-repository grants declared on the signature; the signature *is* the permission surface. |
| **[Placements](placements.md)** | Where a run's body executes — and therefore whether its grants are OS-enforced or advisory. |

How they interlock: a **task** declares what should happen and what it may
touch; a retained **run** executes it and records everything; the record is
populated by the **effects** that crossed the boundary; **permissions** bound
those effects, and the **placement** decides how that bound is enforced.

Two former pillar pages — *Workspaces* and *Providers* — taught the ambient
model-call surface, which has not shipped; they return when it does. Where
that surface sits on the road is mapped on
[Settlement Core / Dataflow](../roadmap.md).

## If you came here to build

You do not need this section to ship your first run — the
[quickstart](../start/index.md) gets you to working code without it. Come
back when something surprises you, and enter through the question that
brought you:

- "Why did editing a *docstring* change behavior?" → [Tasks](tasks.md)
- "What did that run actually *do*?" → [Runs](runs.md)
- "Who was allowed to write what, and who enforced it?" →
  [Permissions](permissions.md) and [Placements](placements.md)
- "What crossed the boundary along the way?" → [Effects](effects.md)

Each page is written to stand alone; cross-links fill whatever gaps remain.

## If you came here to evaluate

Read [tasks](tasks.md), [runs](runs.md), [permissions](permissions.md), and
[placements](placements.md) in that order — the unit of work, its record,
its authority, and its enforcement — then [effects](effects.md) for the
channel underneath. For what is deliberately *not* claimed yet (ambient model
service, returned handles, task-as-value delegation), read
[Settlement Core / Dataflow](../roadmap.md).
