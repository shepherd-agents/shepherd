# Test Shepherd code

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

**Job.** Write automated tests for your model-backed tasks that run offline and
deterministically, asserting on typed values, with no live calls and no
flakiness.

**Prerequisites.** `pytest` and the tutorial environment. The deterministic
offline provider ([deterministic demo](deterministic-demo.md)) is what tests
run against.

## Steps

1. **Call the task inside a workspace, exactly as your program does.** Pin the
   offline provider and call the task in the test body:

    ```python
    import shepherd as sp

    from app import SAMPLE_DIFF, Triage, triage_change


    def test_triage_matches_contract():
        with sp.workspace(model="claude:sonnet-4-5"):
            triage = triage_change(SAMPLE_DIFF)
        assert isinstance(triage, Triage)
        assert (triage.category, triage.priority) == ("bugfix", "high")
    ```

2. **Assert on the typed value, not on parsed text.** The return type *is* the
   contract: the task hands back a real `Triage`, so your test reads fields
   (`triage.category`) instead of scraping a string. There is no JSON parsing to
   assert around.

3. **Test the failure contract too.** Shepherd's typed failures are part of the
   behavior you can pin. Two are worth a test each:

    ```python
    import pytest


    def test_bodyless_task_requires_docstring():
        with pytest.raises(TypeError, match="docstring or guidance"):

            @sp.task
            def nameless(x: str) -> str:  # no docstring -> rejected at definition
                pass


    def test_task_outside_workspace_refuses():
        with pytest.raises(RuntimeError):
            triage_change(SAMPLE_DIFF)  # no workspace open -> no default model
    ```

## Expected result

The tests pass offline and deterministically, the same way the docs' own
examples are tested: a test runs each program and asserts its documented output.
What the docs show is what runs, because a test runs it.

## If it fails

- **`sp.DeliveryFailed`?** The recorded answer could not be coerced into the
  declared return type, usually a return type or docstring that no longer
  matches the contract. Tighten the type, re-run.
- **`RuntimeError` about a workspace?** A task was called outside
  `with sp.workspace(...)`; open one in the test, as in step 1. See
  [Debug your first run](debug-your-first-run.md).
- **Output varies between runs?** A live provider slipped in. Keep tests on the
  offline provider, that is what makes them deterministic.
