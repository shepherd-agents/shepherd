# Configure a provider

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
    The `shepherd provider` CLI (`login`, `check`, `list`) is the **planned**
    credential-management surface and has not shipped. Selecting a provider in
    code (step 3) is real today; the CLI steps below preview the planned shape.

**Job.** Record credentials for a model provider once, verify Shepherd can
reach it, and select its models per workspace in code.

**Prerequisites.** `shepherd-ai` installed ([install](../start/install.md)); a
provider account and API key. **Live providers cost money and are
non-deterministic.** The offline examples need none of this.

## Steps

The `shepherd provider ...` commands below are the planned CLI surface,
**unshipped**, shown here as a preview of the intended workflow.

1. **Log in once.** Records the credential (or a reference to it) in the
   configured credential store, not in your repository:

    ```bash
    shepherd provider login claude
    ```

    Prefer the credential store. If you must use an environment variable,
    commit only placeholders: `ANTHROPIC_API_KEY=<your-key>`.

2. **Verify without running anything.** Read-only checks of the installed
   SDK, the credential, and provider reachability:

    ```bash
    shepherd provider check claude
    shepherd provider list --json
    ```

3. **Select the model in code.** Real today: import the provider entry
   point; the workspace pins the model for every task call inside the block:

    ```python
    import shepherd as sp

    with sp.workspace(model="claude:sonnet-4-5"):
        ...  # every task call in here uses that model
    ```

## Expected result

- `shepherd provider check claude` reports the SDK installed, a credential
  present, and the provider reachable (planned contract; unshipped).
- A task called inside the workspace runs against the selected model; the
  same code targets a different model by changing only `model="claude:..."`.

## If it fails

- **No credential found**, re-run `shepherd provider login claude`; check
  for an environment variable set in one shell but not another.
- **The task raises before any model call**, it was called outside
  `with sp.workspace(model=...)`; see
  [Debug your first run](debug-your-first-run.md).
- **Cost or flaky-output surprises**, live calls are billed and vary run to
  run; keep your tests on the deterministic offline provider.
