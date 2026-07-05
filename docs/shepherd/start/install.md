# Install

> Page status: fast-follow
> Source state: preview
> Applies to: Shepherd v0.2.0
> Owner: @docs-system-owner (TBD)
> Validation: scripts/check_shepherd_docs.py

*Quickstart. This page is the install step. The quickstart and tutorial build on it. For exact APIs, see the reference.*

## Requirements

- Python **3.11+**.
- No provider credentials for the offline path. One live provider key (for
  example `ANTHROPIC_API_KEY=<your-key>`) only when you opt into live runs.

## Pick a distribution

| You want | Install | What you get |
|---|---|---|
| The product (tutorial path) | `pip install shepherd-ai` | The `shepherd` import package, the `shepherd` CLI, the local run path, the provider registry, the deterministic offline provider, and one live provider path. |
| Slim / audit install | `pip install shepherd-base` | The `shepherd` import package, the `shepherd` CLI, a slim runtime, and the offline provider, without the live-provider SDK. |
| First-party workflows | `pip install "shepherd-ai[authoring]"` | Adds packaged workflow plugins on top of the product. |

The tutorial and Getting Started path is always `shepherd-ai`, never
`shepherd-base`. `shepherd-ai` is the *distribution* name; the *import* is:

```python
import shepherd as sp
```

## Next

- [Getting Started](index.md): the path that works today.
- [Your first Shepherd app](../tutorials/first-shepherd-app.md): the tutorial.
