# Your first Shepherd app

> Page status: fast-follow
> Source state: checked-example
> Applies to: Shepherd v0.2.0
> Owner: @docs-system-owner (TBD)
> Validation: docs_src/tutorials/first_app/test_first_app.py

*Tutorial. A learning path, in order. For task-specific recipes, see the guides. For exact APIs, see the reference.*

!!! warning "Not published — docs firewall (2026-07-06)"
    This page teaches (or routes readers into) the ambient model-call idiom —
    `with sp.workspace(model=...): task(...)` — which does not run on the
    shipped `shepherd-ai` 0.2.0 wheel. It is retained as source material for a
    future rewrite and is excluded from the published site until the surface
    it teaches actually ships. Do not re-add it to the public nav until then.
    What ships today, and the named road, are mapped on
    [Settlement Core / Dataflow](../roadmap.md).

> **Achievement.** Build and run a two-task change reviewer: one task that
> classifies a code change, one that reviews it, composed with plain Python.
> ~30–40 minutes.
>
> **Prerequisites.** Python 3.11+; comfortable with dataclasses and type
> hints. No prior agent-framework experience assumed.

Every code block on this page comes from one tested program, and it runs
against a recorded, deterministic offline provider, so what you read is what
runs.

## §1. What you'll build

A change reviewer made of two model-backed tasks and one ordinary function:

- `triage_change(diff) -> Triage`, classifies a code change: category,
  priority, rationale.
- `write_review(diff, triage) -> Review`, writes a short review, given the
  diff and its triage.
- `review_change(diff) -> Review`, plain Python that feeds the first task's
  output into the second.

Small as it is, this is the real shape of a Shepherd program: typed functions
where you want a model, ordinary Python everywhere in between.

## §2. Your first task

Start with the imports and the type you want back:

```python
--8<-- "tutorials/first_app/app.py:setup"
```

`Triage` is a frozen dataclass, three string fields and two comments. It is
not boilerplate; it is the **contract**. Whatever the model says, your code
only ever receives a real `Triage` instance with those fields, or an error.

Now the task:

```python
--8<-- "tutorials/first_app/app.py:triage"
```

That function has no body, and it does not need one. In Shepherd the
**signature carries the meaning**:

- **Parameters are the inputs.** `diff: str` tells Shepherd to hand the model
  a string named `diff`. You never format a prompt by hand, each parameter
  is rendered for the model in a way appropriate to its type.
- **The return type is the validated contract.** `-> Triage` becomes the
  response schema. The reply is checked and coerced into a `Triage`, or the
  call raises. There is no JSON parsing anywhere in your code.
- **The docstring is the instruction.** The first line is the job; the rest
  is elaboration. Write it the way you would brief a careful colleague:
  plain English, the categories named, the judgment call made explicit
  ("Priority reflects user impact, not engineering effort.").

One consequence is worth pausing on: that docstring is **behavior, not a
comment**. It is what the model is actually asked to do, so editing it
changes what your program does, and a bodyless task without one is rejected
when the decorator runs, not silently accepted.

!!! success "Checkpoint"
    You have a typed, model-backed function. Prove the docstring rule to
    yourself: delete the docstring and re-import the module, `@sp.task`
    raises `TypeError` at definition time, because a bodyless task with no
    instruction is meaningless. Put it back.

## §3. Run it in a workspace

A task does not choose its own model. That is the job of the **workspace**,
the ambient context that every task call inside the block inherits:

```python
--8<-- "tutorials/first_app/app.py:run"
```

This block is the entry point of the finished program, which is why it is
indented, it sits inside the file's `main()`. Two things to notice:

- `sp.workspace(model="claude:sonnet-4-5")` pins the model once, at the
  top. The tasks themselves stay model-agnostic: change that one argument and
  the same tasks run against a different model.
- The second call, `review_change`, is the composed reviewer you build in
  §4. The file ships complete, so you are meeting the entry point one section
  early.

Calling a task **outside** any workspace fails fast with an error telling you
to open one. There is no hidden default model and no accidental network call.

`SAMPLE_DIFF`, defined in the same file, is a one-line change to an admin
check:

```text
diff --git a/auth.py b/auth.py
@@ -42,7 +42,7 @@
-    if user.is_admin:
+    if user.is_admin or user.has_role("admin"):
```

Run the program:

```bash
python app.py
```

**Expected output**

```text
bugfix/high: approve - Tightens the admin gate in auth.py by requiring an explicit role check; low blast radius, no API change.
```

The line is deterministic because the offline provider replays recorded
transcripts, so this page stays in step with the code you run.

!!! success "Checkpoint"
    The program ran end to end, and `triage` is a real `Triage` instance:
    `triage.category == "bugfix"`, `triage.priority == "high"`, and
    `triage.rationale` is a sentence you can read. Typed in, typed out, no
    parsing code anywhere in the file.

## §4. Compose a second task

The reviewer needs a second task, and something to connect the two:

```python
--8<-- "tutorials/first_app/app.py:review"
```

Three small pieces:

- `Review` is another frozen dataclass, the second contract.
- `write_review` is another bodyless task. Look at its second parameter:
  `triage: Triage`. Tasks can take **structured inputs**, including the typed
  output of another task; Shepherd renders the dataclass's fields to the
  model as labeled inputs, the same way it rendered `diff`.
- `review_change` is the composition, and it is **not** a task. It is a
  plain function with a single line of ordinary Python: call `triage_change`,
  pass the result to `write_review`.

This is the payoff of tasks being functions. **Composition is function
call.** There is no pipeline object, no graph DSL, no orchestration
framework: a reader who has never seen Shepherd can still read
`write_review(diff, triage_change(diff))` correctly. And because each task is
an independent typed function, you can move the two tasks to different
modules, test them separately, or reuse `triage_change` in another program
tomorrow.

When one task genuinely needs several model calls with control flow between
them, you can give it a body and sequence the calls yourself, a later
tutorial covers that. Most tasks look like the two on this page.

!!! success "Checkpoint"
    `review_change(SAMPLE_DIFF)` returns a `Review` with
    `verdict == "approve"` and a summary that names `auth.py`. This is exactly
    what the tutorial's own test asserts, so the behavior is pinned, not
    aspirational.

## §5. What's next

You have built the core Shepherd shape: typed contracts (`Triage`,
`Review`), bodyless tasks whose docstrings are the instructions, one
workspace pinning the model, and composition in plain Python.

From here:

- **[Concepts: Tasks](../concepts/tasks.md)**, the mental model behind what
  you just did: why signatures carry meaning and where the boundaries sit.
- **[Guides](../guides/index.md)**, task-focused recipes for deterministic
  runs, debugging, testing, and routing tasks to models.
