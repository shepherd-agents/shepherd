# DRAFT — pinned discussion: "State of the project" (do not publish from here)

> Status: draft text for a pinned GitHub Discussion (tranche plan A8).
> To be posted by a maintainer after the Gate-0 sweep, alongside the docs
> firewall wave. This file is a source draft, not a published page — it lives
> outside `docs/shepherd/` so it is not part of the site build.

---

## State of the project (read this first)

Shepherd is in **early alpha** (0.2.0 on PyPI), and it had a louder week than
we planned for. This pin is the honest orientation: what actually ships, what
is on the road, and how to read the docs' claims.

### The one-paragraph version

Shepherd 0.2.0 is a **settlement machine** for agent work. A task is a typed
Python function whose signature carries its permissions
(`May[GitRepo, ReadWrite]`); running it as a **retained run**
(`workspace.run(...)`) executes it — offline and deterministic on the
`static` provider, or as a live sandboxed agent on `claude` — and its work
comes back as a **retained output** beside your files: inspect the
per-binding changeset, read the contents, and settle it exactly once
(`select` / `release` / `discard`). Every run leaves a durable trace
(`shepherd run trace`). That loop runs on the shipped wheel today, and the
[quickstart](https://docs.shepherd-agents.ai/start/) walks it end to end.

### What is deliberately NOT in 0.2.0

The composable "meta-agents in plain Python" surface is the north star, not
the shipped release. Specifically, these are **Dataflow road** items — they
do not run on 0.2.0:

- ambient model service for direct task calls
  (`with sp.workspace(model=...): my_task(...)` — fails loudly today);
- returned handles (tasks returning `GitRepo`-like values);
- typed value projection from captured work;
- threading and durable children;
- **task-as-value delegation** — the `oversee(implement, ...)` supervised
  meta-agent shape. Explicitly deferred.

The full map, with each entry tagged, is the
[Settlement Core / Dataflow roadmap page](https://docs.shepherd-agents.ai/roadmap/).

### An apology about the docs, and the fix

Some earlier published pages taught the ambient idiom ahead of the wheel —
code that read beautifully and raised `DeliveryFailed` when you ran it. That
was our mistake. We have since run a **docs firewall**: pages teaching
unshipped surfaces were pulled from the published site, the quickstart was
replaced with one that runs verbatim on the shipped wheel (its published
transcript is asserted by an executed test), and the standing rule is now:

> **Every published sentence runs on the shipped wheel, or is explicitly
> labeled** (roadmap / simulated / illustrative).

If you find an unlabeled exception, that is a bug in the docs — please file
it with the "Docs: a published claim doesn't run on the wheel" issue form.

### How to report "X doesn't work"

Use the issue forms; they route on one question — *is X claimed as shipped?*

1. **Published page says it works, and it doesn't** → "Bug report" form
   (include what you ran, the claim it contradicts, and your environment).
2. **It's listed under Dataflow on the roadmap** → it hasn't shipped; ask or
   argue here in this discussion instead — sequencing feedback is genuinely
   useful.
3. **Docs sentence is wrong or unlabeled** → the docs-claim form.

### Platforms

Python **3.11+**. Grant enforcement is exercised on **macOS** (Seatbelt);
**Linux** Landlock enforcement is container-gated today; **Windows** is
unsupported — use WSL.

### What's next

Near-term work is focused on correctness and honesty of the shipped surface
(bugfixes, louder failures where behavior could mislead) and on the
settlement/review loop. We announce surfaces when they run, not before;
watch the roadmap page — its labels move only on executed evidence.
