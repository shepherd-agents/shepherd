# Reference

> Page status: scaffold
> Source state: generated
> Applies to: Shepherd v0.3.0
> Owner: @docs-system-owner (TBD)
> Validation: scripts/gen_shepherd_api_inventory.py --check

*Reference. Exact, generated facts. The mental model lives in concepts, recipes in guides.*

!!! warning "Early API"
    These pages are generated straight from the `shepherd` package, so they match the code today.
    The API is still taking shape so please expect names and signatures to change before a stable release.

The everyday surface is the top-level facade:

```python
import shepherd as sp
```

## What's here

- **Per-symbol API pages** (`api/`) — one page per public facade symbol,
  taken from the real source docstrings so they match the code today.
- **[CLI](cli.md)** — the command-line reference; help blocks captured
  verbatim from the shipped `shepherd` / `sp` CLI.

