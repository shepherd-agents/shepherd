# Getting Started

> Page status: release-ready
> Source state: checked-example
> Applies to: Shepherd v0.1.1-dev
> Owner: @docs-system-owner (TBD)
> Validation: docs_src/quickstart/test_hello.py

*Quickstart. To learn the concepts in order, see the tutorial. For exact APIs, see the reference.*

A worker task and a meta-agent that supervises it. One deterministic run.

## Install

```bash
pip install shepherd-ai
```

Every example on this site runs against a recorded offline provider. No credentials, no network.

## Run

Save this as `hello.py` and run `python hello.py`:

```python
--8<-- "quickstart/hello.py:hello"
```

Two things are happening here:

- `implement` is an ordinary task: a typed Python function with a docstring and no body. The docstring is the instruction the model gets. The `-> str` return type is the contract the answer must match. `@sp.task` makes it runnable.
- `oversee` is a meta-agent, which is just another task. It takes `implement` as an argument and runs it. If the tests fail, it reverts and retries. That's the idea: a meta-agent is a function that takes your agents as input.

`sp.workspace(model=claude("sonnet-4-5"))` pins the model every task call in the block runs against.

## Expected output

```text
Login feature landed. The worker's first attempt failed 2 tests, so oversee reverted that step and retried; the retry passed all 41.
```

The output is deterministic. The offline provider replays a recorded transcript, so what you read here is exactly what runs.

## If it fails

- **Called a task outside the `with` block?** Shepherd won't run a task with no workspace configured. It raises right away and tells you to open one. There's no hidden default model. Move the call inside `with sp.workspace(...)`.
- **`sp.DeliveryFailed`?** The response didn't coerce to the declared return type. Against the offline provider on this example, that means a broken install. Reinstall and rerun.

## Next

Two tasks, composed into a reviewer:

[Your first Shepherd app →](../tutorials/first-shepherd-app.md)
