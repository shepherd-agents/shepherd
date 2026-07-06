# Tutorials

> Page status: fast-follow
> Source state: shipped-source
> Applies to: Shepherd v0.2.0
> Owner: @docs-system-owner (TBD)
> Validation: scripts/check_shepherd_docs.py

*Tutorial. A learning path, in order. For task-specific recipes, see the guides. For exact APIs, see the reference.*

!!! warning "Not published — docs firewall (2026-07-06)"
    This page teaches (or routes readers into) the ambient model-call idiom —
    `with sp.workspace(model=...): task(...)` — which does not run on the
    shipped `shepherd-ai` 0.2.0 wheel. It is retained as source material for a
    future rewrite and is excluded from the published site until the surface
    it teaches actually ships. Do not re-add it to the public nav until then.
    What ships today, and the named road, are mapped on
    [Settlement Core / Dataflow](../roadmap.md).

The tutorial track teaches Shepherd **in order**: each page builds on the one
before, and each ends with something you ran yourself. You start with a typed
task and a workspace and finish with a small composed program; later pages,
effects, handlers, traces, supervision, arrive as those surfaces ship
publicly.

Available now, tested and deterministic:

- **[Your first Shepherd app](first-shepherd-app.md)**, a two-task change
  reviewer in ~30–40 minutes. (That page is release-ready and tested.)

## Which kind of page do you need?

These docs keep four page kinds strictly apart, so each can keep its promise:

| You are asking | Read a | The page's promise |
|---|---|---|
| "Teach me, in order." | **Tutorial** | A learning path: ordered steps, checkpoints, one running example. It teaches the happy path; it does not try to cover every option. |
| "How do I do this one job?" | **Guide** | A recipe for a named task: prerequisites, steps, expected result, failure notes. It assumes you know the basics. |
| "Why is it like this?" | **Concept** | The mental model, vocabulary, boundaries, tradeoffs. No steps to follow. |
| "What exactly does this API do?" | **Reference** | Exact, checked facts: signatures, types, errors. Generated or verified, never narrative. |

A tutorial is not a long guide, and a concept page is not a slow tutorial,
if a page mixes those jobs, that is a bug in the docs.

## Start here

[Your first Shepherd app →](first-shepherd-app.md)
