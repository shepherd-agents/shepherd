# `shepherd.May`

> Page status: scaffold
> Source state: generated
> Applies to: Shepherd v0.2.0
> Owner: @docs-system-owner (TBD)
> Validation: scripts/gen_shepherd_api_inventory.py --check

*Reference. Exact, generated facts. The mental model lives in concepts, recipes in guides.*

<span class="api-kind">handle-surface (runtime-resolved)</span>

`shepherd.May` is part of the workspace-handle surface: it is
resolved lazily at runtime because its implementation imports the
substrate engine, which the offline docs build does not load.

- Runtime source: `shepherd_dialect.workspace_control.May`
- Usage and semantics: [Permissions](../../concepts/permissions.md)
  and the run/output/settlement examples in the guides.
