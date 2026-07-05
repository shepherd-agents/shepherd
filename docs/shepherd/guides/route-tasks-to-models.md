# Route tasks to models

> Page status: release-ready
> Source state: checked-example
> Applies to: Shepherd v0.2.0
> Owner: @docs-system-owner (TBD)
> Validation: pytest docs_src/shepherd/tutorials/

*How-to guide. New to Shepherd? Start with the tutorial. For exact APIs, see the reference.*

**Job.** Run different tasks against different models, a cheap, fast model for
the easy step, a stronger one for the hard step, without editing the tasks
themselves.

**Prerequisites.** The tutorial environment and providers selectable in code.

## Steps

1. **Remember the rule: the workspace pins the model, the task does not.** A
   task is model-agnostic, its signature never names a model. Whichever
   workspace is open when you call it decides who answers. So "routing" is not a
   setting on the task; it is *which workspace you call it in*.

2. **Scope each call to the model you want.** Open one workspace per model and
   call the relevant task inside it:

    ```python
    import shepherd as sp

    # cheap, fast model for the easy classification step
    with sp.workspace(model="claude:haiku-4-5"):
        triage = triage_change(diff)

    # stronger model for the step that needs more judgment
    with sp.workspace(model="claude:sonnet-4-5"):
        review = write_review(diff, triage)
    ```

    The same two tasks would run against one model if you opened one workspace
    around both calls. Nothing in `triage_change` or `write_review` changed,
    only the scope each was called in.

3. **Pass results across scopes as ordinary values.** `triage` is a plain typed
   value once the first workspace closes; handing it to the second task in the
   next workspace is normal Python. Routing lives in the call sites, not in a
   pipeline object.

## Expected result

Each task runs against the model of its enclosing workspace; switching a step to
another model is a one-line change to that block's `model="claude:..."` argument, and
the tasks stay untouched. Workspace-pins-the-model is the same behavior the
tutorial exercises and tests.

## If it fails

- **`RuntimeError` about a workspace?** A task was called between blocks, with no
  workspace open. Every task call must sit inside a `with sp.workspace(...)`;
  see [Debug your first run](debug-your-first-run.md).
- **Both tasks ran on the same model?** They were inside the same workspace. Give
  each its own block, as in step 2.
