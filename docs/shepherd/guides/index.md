# Guides

> Page status: release-ready
> Source state: shipped-source
> Applies to: Shepherd v0.3.0
> Owner: @docs-system-owner (TBD)
> Validation: scripts/check_shepherd_docs.py

*How-to guide. New to Shepherd? Start with the quickstart. For exact APIs, see the reference.*

Guides are **task recipes**: each does one named job and keeps one shape —
job, prerequisites, steps, expected result, and what to do when it fails.
They assume you have already met Shepherd (start with the
[quickstart](../start/index.md) if not).

Available now, running on the shipped 0.3.0 wheel:

- [Grant a task repository access](grant-repo-access.md) — per-repository
  read-only / read-write grants declared in the signature, enforced at the OS
  under a jailed placement, with per-binding changesets and explicit
  settlement.

This list is deliberately short. Several earlier guides taught the ambient
model-call surface (`with sp.workspace(model=...): task(...)`), which has not
shipped; they were pulled from the published site until the surface they
teach runs on the wheel, and they will return with it. What ships today
versus what is on the named road is mapped on
[Settlement Core / Dataflow](../roadmap.md).
