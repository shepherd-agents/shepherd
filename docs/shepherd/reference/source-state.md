# Source-state inventory

> Page status: release-ready
> Source state: shipped-source
> Applies to: Shepherd v0.3.0
> Owner: @docs-system-owner (TBD)
> Validation: scripts/check_shepherd_docs.py
> Stale-names: migration-context

*Source-state inventory. A hand-maintained record of what these docs can claim today, and where each fact comes from.*

This prototype exercises the full documentation pipeline. The public build is
limited to pages whose current claims are backed by checked examples, shipped
source, or an explicit source-state row. Additional scaffold pages may exist in
the internal reviewer build; they are excluded from the public site.

| Fact family | Source of truth today | State |
|---|---|---|
| Python API reference (41 public symbols) | **Real**: the `shepherd` integration facade (`shepherd/packages/meta/src/shepherd/__init__.py`), read statically by the generator; docstrings render from the actual runtime sources. | `generated`, internal build only. |
| API symbol snapshot + drift check | **Real**: `_generated/python-api/public-symbols.json`, regenerated and byte-compared by `5_check_everything_is_ok.sh`. | `generated` |
| Tutorial + quickstart example code | **Real code, simulated provider**: `docs_src/` examples execute in pytest against the simulation shim (`docs_src/_sim/`). Pages include this code via snippets, what you read is what ran. | `checked-example` |
| Concepts (tasks, effects, permissions, placements, runs, workspaces, providers) | Distilled from `docs/paradigm.md`, the spec, the curriculum, and the current facade pages. Per-binding **permissions** and **placements** are shipped (0.2.0, extended in 0.3.0) and are documented as real; live-provider operations and workflow *packaging catalogs* remain the operations public pages avoid claiming until their surfaces land. | `shipped-source` / conceptual public pages |

## How a row changes

When a real source lands, the same PR updates the source, regenerates the
affected pages (`5_check_everything_is_ok.sh` fails otherwise), and flips this row, see the
runbook scenarios (S2, S7) in `docs/_runbook.md`.
