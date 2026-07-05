# Debug your first run

> Page status: release-ready
> Source state: checked-example
> Applies to: Shepherd v0.2.0
> Owner: @docs-system-owner (TBD)
> Validation: pytest docs_src/shepherd/quickstart/ docs_src/shepherd/tutorials/

*How-to guide. New to Shepherd? Start with the tutorial. For exact APIs, see the reference.*

**Job.** Your first run failed; identify which of the three classic
first-run failures you hit, and fix it.

**Prerequisites.** You attempted the [Getting Started](../start/index.md) walkthrough or
the [tutorial](../tutorials/first-shepherd-app.md).

## Steps

1. **Read the exception type, not just the message.** Shepherd fails with
   typed errors, and the type names the layer that failed: task definition
   (`TypeError`), missing context (`RuntimeError`), or the model's response
   (`sp.DeliveryFailed`).

2. **Match it in the table.** All three rows are real, tested behaviors:

    | What you see | Why | Fix |
    |---|---|---|
    | ``RuntimeError: call tasks inside `with sp.workspace(model=...)` `` | The task was called with no workspace open. There is no default model and no accidental network call, Shepherd refuses instead. | Wrap the call: `with sp.workspace(model="claude:sonnet-4-5"): ...` |
    | `sp.DeliveryFailed: ...` | The model's response could not be coerced into the declared return type, missing dataclass fields, or the wrong shape where `-> str` was promised. The message names what was missing. | Tighten the return type and docstring so the contract is unambiguous, then rerun; the docstring is the instruction the model is following. |
    | `TypeError: Bodyless callable task ... must declare a docstring or guidance=` | A bodyless `@sp.task` has no docstring. The docstring **is** the model-call goal, so omitting it is an error at definition time, not a silent no-op. | Write the docstring: first line is the job, the rest is elaboration. |

3. **Re-run the [Getting Started](../start/index.md) and
   [tutorial](../tutorials/first-shepherd-app.md) examples** to confirm your
   environment is sound.

## Expected result

The failing call completes: the Getting Started page prints its three bullets,
and the tutorial prints its `bugfix/high: approve - ...` line.

## If it fails

- A fourth, different error? You may be using a surface these docs don't
  cover yet.
