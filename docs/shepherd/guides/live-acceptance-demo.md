# Live acceptance demo

> Page status: fast-follow
> Source state: preview
> Applies to: Shepherd v0.3.0
> Owner: @docs-system-owner (TBD)
> Validation: scripts/check_shepherd_docs.py

*How-to guide. New to Shepherd? Start with the tutorial. For exact APIs, see the reference.*

<!-- FIREWALL SOURCE SWEEP (2026-07-06): this page's "real today" claims about
selecting a provider in code refer to the ambient `workspace(model=...)` idiom,
which does not run on the shipped 0.3.0 wheel (the direct task call raises
DeliveryFailed; no ambient servicer ships in 0.3.0). Re-verify every claim
against the shipped wheel before promoting this page. See
docs/shepherd/roadmap.md (Settlement Core / Dataflow). -->

!!! warning "Not shipped yet"
    This prototype is **offline by design**, every documented example runs
    against the deterministic offline provider. The live-run path described here
    is **planned**, shown as a preview of its intended shape. **Live calls cost
    money and are non-deterministic.** Keep everyday development on the offline
    provider ([deterministic demo](deterministic-demo.md)).

**Job.** Once, before you rely on offline development, confirm that real
credentials, the network, and a live model all work end to end: a single live
run that returns a valid typed result. This is an *acceptance* check, not a
development loop.

**Prerequisites.** A provider account and API key, recorded per
[Configure a provider](configure-provider.md); `shepherd-ai` installed
([install](../start/install.md)). Expect a small, real charge.

## Steps

The live switch below is the **planned** surface, shown as a preview of the
intended workflow.

1. **Record a credential** (planned `shepherd provider login`), see
   [Configure a provider](configure-provider.md). The offline provider needs
   none of this; a live run does.

2. **Select a live provider in code.** Selecting the provider is real today; the
   switch that routes it to the live backend instead of the recorded transcript
   is the planned part:

    ```python
    import shepherd as sp

    with sp.workspace(model="claude:sonnet-4-5"):
        review = review_change(SAMPLE_DIFF)   # a real, billed model call (planned)
    ```

3. **Run it once and read the result, not the wording.** A live answer varies
   run to run. You are accepting that a real call *connects and returns a valid
   `Review`*, not that it prints any exact sentence. Do not assert on the text.

## Expected result

A typed result comes back from a genuine model call, and a charge appears on
your provider dashboard. From here, switch back to the offline provider for
day-to-day work and tests, acceptance is a one-time gate, not the inner loop.

## If it fails

- **No credential / not reachable?** See
  [Configure a provider](configure-provider.md); a credential set in one shell
  may be missing in another.
- **Cost or flaky-output surprises?** Expected, live calls are billed and
  non-deterministic. That is the reason the deterministic offline provider is
  the default everywhere else.
