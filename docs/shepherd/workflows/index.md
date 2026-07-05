# Workflows

> Page status: scaffold
> Source state: scaffold
> Applies to: Shepherd v0.2.0
> Owner: @docs-system-owner (TBD)
> Validation: not yet validated

*Operators. Packaged workflows ship with the Shepherd CLI. Until then, this page lists what operators can read today.*

Shepherd will ship first-party, packaged workflows, install them, configure
credentials, and run them in CI without writing any task code:

```text
pip install "shepherd-ai[authoring]"
shepherd workflow run official.authoring.pr_review
```

That surface has **not shipped yet**. This catalog will be generated from
workflow manifests when it does; until then there is nothing to install, and
this page will not pretend otherwise.

## What operators can read today

- [Concepts → Tasks](../concepts/tasks.md), what a workflow is made of.
- [The source-state inventory](../reference/source-state.md), the ledger of
  what exists now versus what is coming.
- [First Shepherd app](../tutorials/first-shepherd-app.md), the builder path,
  if you want to see what workflow authors write.
