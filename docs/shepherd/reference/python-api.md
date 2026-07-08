# Python API

> Page status: scaffold
> Source state: generated
> Applies to: Shepherd v0.3.0
> Owner: @docs-system-owner (TBD)
> Validation: scripts/gen_shepherd_api_inventory.py --check

*Reference. Exact, generated facts. The mental model lives in concepts, recipes in guides.*

!!! warning "Early API"
    This is the entry page for the Python API reference. The per-symbol pages
    under `reference/api/` come straight from the public facade docstrings. The
    API is still taking shape — expect names and signatures to change before a
    stable release.

Shepherd's public surface is a small facade: you `import shepherd as sp` and
reach the task/workspace/delivery spine plus the effect and run vocabulary. Only
the public facade appears in this reference — internal implementation packages
are not documented here just because they happen to be importable.

The per-symbol pages live under `reference/api/` — one page per public symbol
(see [`task`](api/task.md) for the shape).
