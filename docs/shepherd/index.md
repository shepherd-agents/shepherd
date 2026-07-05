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
> Source state: shipped-source
> Applies to: Shepherd v0.2.0
> Owner: @docs-system-owner (TBD)
> Validation: scripts/check_shepherd_docs.py
-->

<div class="shp-hero" markdown>

# Program meta-agents in Python

Write agents as simple typed functions, and meta-agents as functions that take your agents as input.

[Get started](start/index.md){ .md-button .md-button--primary }

</div>

```python title="hello.py"
--8<-- "quickstart/hello.py:hello"
```

## Find your path

<div class="grid cards" markdown>

-   :material-rocket-launch:{ .lg .middle } **Build your first agent**

    ---

    A typed task, a workspace, and a small working reviewer. Offline and
    deterministic.

    [:octicons-arrow-right-24: First Shepherd app](tutorials/first-shepherd-app.md)

-   :material-bug-check:{ .lg .middle } **Debug and test a run**

    ---

    Read typed failures, keep runs deterministic, and test model-backed
    code without live calls.

    [:octicons-arrow-right-24: Debug your first run](guides/debug-your-first-run.md)

-   :material-lightbulb-on:{ .lg .middle } **Understand & evaluate**

    ---

    The mental model behind tasks, effects, and runs - and how they fit together into one framework.

    [:octicons-arrow-right-24: Concepts: Tasks](concepts/tasks.md)

</div>

## Why Shepherd

- **Typed.** A task is a function with a signature and a docstring. The return
  type is the contract the model must satisfy.
- **Observable.** Every run records what was sent and returned, so you debug by
  reading a trace instead of guessing.
- **Composable.** Tasks are values. Pass them, supervise them, and build bigger
  programs out of small ones.

<br>

!!! info "Important"
    Shepherd is an early **development preview** - ready to explore and build
    with, but not yet to depend on. Expect **breaking changes** between releases and rough edges as the design
    settles, and please don't build production or business-critical workflows on
    it yet. Support is best-effort, and nothing is guaranteed to be stable while
    we're pre-1.0. If something is missing, confusing, or broken, please [let us know](https://github.com/shepherd-agents/shepherd/issues).
