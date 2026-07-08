# Deterministic demo

> Page status: fast-follow
> Source state: checked-example
> Applies to: Shepherd v0.3.0
> Owner: @docs-system-owner (TBD)
> Validation: pytest docs_src/shepherd/quickstart/ docs_src/shepherd/tutorials/

*How-to guide. New to Shepherd? Start with the tutorial. For exact APIs, see the reference.*

!!! warning "Not published — docs firewall (2026-07-06)"
    This page teaches (or routes readers into) the ambient model-call idiom —
    `with sp.workspace(model=...): task(...)` — which does not run on the
    shipped `shepherd-ai` 0.3.0 wheel. It is retained as source material for a
    future rewrite and is excluded from the published site until the surface
    it teaches actually ships. Do not re-add it to the public nav until then.
    What ships today, and the named road, are mapped on
    [Settlement Core / Dataflow](../roadmap.md).

**Job.** Run a Shepherd example with no credentials and no network, and get
**identical output every time**, using the deterministic offline provider that
every documented example runs against.

**Prerequisites.** The quickstart or tutorial environment. No API key, no
account, nothing billed.

## Steps

1. **Use the offline provider, it is the default for every documented
   example.** Calls are answered from a recorded transcript, so the run is
   deterministic and offline. The quickstart program is the smallest case:

    ```python
    --8<-- "quickstart/hello.py:hello"
    ```

    The workspace pins `model="claude:sonnet-4-5"`, but against the offline provider
    the answer is replayed, not generated, no credential is read and no request
    leaves the machine.

2. **Run it twice.**

    ```bash
    python hello.py
    python hello.py
    ```

    **Expected output (both runs, identical)**

    ```text
    The login plan covers the core flow and names the right tests. Risks: no rate limit on failed attempts, and session fixation is not addressed — rotate the session id at sign-in. Add an expired-credential test. With those noted, the login plan passed review: approve.
    ```

## Expected result

The two runs print the same review, character for character. Determinism
is the point: the offline provider replays a recorded transcript, so what you
read here is what runs.

## If it fails

- **Output differs between runs?** You are not on the offline provider. The
  documented examples select it by default; check you did not swap in a live
  model.
- **`sp.DeliveryFailed`?** On this example, against the offline provider, that
  signals a broken install, reinstall and rerun.
- **`RuntimeError` about a workspace?** The task was called outside
  `with sp.workspace(...)`; see [Debug your first run](debug-your-first-run.md).
