---
hide:
  - navigation
  - toc
---

<!--
Page-metadata block, kept in an HTML comment so the membership gate
(scripts/check_shepherd_docs.py) still reads the `> Key: value` lines while the
landing renders without a visible status banner.
> Page status: release-ready
> Source state: checked-example
> Applies to: Shepherd v0.3.0
> Owner: @docs-system-owner (TBD)
> Validation: docs_src/quickstart/test_world_hero.py
-->

<div class="shp-hero" markdown>

# Program meta-agents in Python

Agent work arrives as a **reviewable, reversible proposal**: typed tasks,
permissions in the signature, and retained runs you inspect before you decide.

[Get started](start/index.md){ .md-button .md-button--primary }

</div>

```python title="hero.py — runs on the installed package, offline (after `shepherd init`)"
--8<-- "quickstart/world_hero.py:hero"
```

## Find your path

<div class="grid cards" markdown>

-   :material-rocket-launch:{ .lg .middle } **Run the quickstart**

    ---

    Initialize a workspace, run a task, inspect its retained changeset, and
    settle it. Offline and deterministic, on the installed package.

    [:octicons-arrow-right-24: Getting Started](start/index.md)

-   :material-shield-key:{ .lg .middle } **Permissions in the signature**

    ---

    Per-repository read-only / read-write grants declared on the task's
    parameters — enforced at the OS on a jailed placement.

    [:octicons-arrow-right-24: Grant a task repo access](guides/grant-repo-access.md)

-   :material-map:{ .lg .middle } **What ships vs. what's on the road**

    ---

    Exactly what ships in 0.3.0 — and what's ahead: returned handles and
    task-to-task delegation are on the roadmap, not in this release.

    [:octicons-arrow-right-24: Roadmap](roadmap.md)

</div>

## Why Shepherd

- **Typed.** A task is a Python function: signature, docstring, and — right on
  the parameters — its permission grants. Reading the signature is reading the
  permission surface.
- **Observable.** Every run leaves a durable trace; `shepherd run trace` reads
  back exactly what happened, so you debug by reading a record, not guessing.
- **Reviewable.** A run's work lands as a retained output beside your files,
  inspected per binding and settled explicitly — `select`, `apply`, `release`,
  or `discard` — exactly once. `apply` three-way-settles a kept output onto a
  workspace that has moved on, and fails closed on any overlap.

The composable meta-agent surface — tasks passed to tasks, supervised
retries — is where Shepherd is headed and is **not yet shipped**; the
[roadmap](roadmap.md) says exactly what is.

<br>

!!! info "Important"
    Shepherd is an early **development preview** - ready to explore and build
    with, but not yet to depend on. Expect **breaking changes** between releases and rough edges as the design
    settles, and please don't build production or business-critical workflows on
    it yet. Support is best-effort, and nothing is guaranteed to be stable while
    we're pre-1.0. If something is missing, confusing, or broken, please [let us know](https://github.com/shepherd-agents/shepherd/issues).
