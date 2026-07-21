# shepherd-dialect

The Shepherd dialect over vcs-core's execution-mechanism SPI — the production
**run driver** (`ShepherdRunDriver`), discharged from vcs-core's experimental
staging per the dialect-composes boundary
([`docs/engineering/convergence/execution-boundary.md`](../../../docs/engineering/convergence/execution-boundary.md)).

- vcs-core owns the *mechanisms*: reversible scopes, the confined-launch verb,
  implicit capture at merge, dispatch.
- This package owns *policy + composition*: the `run` command's vocabulary,
  task resolution, the provider seam, and (from B3c) the `may=` →
  `ConfinementSpec` lowering.

Import discipline: only `vcs_core.runtime_api`, `vcs_core.spi`, and
`vcs_core.runtime_substrate` — never `vcs_core._*` or retired
`vcs_core.experimental` homes. The run-path executor guard (PD7) and the
no-private-coupling ratchet point here.

```bash
uv run --package shepherd-dialect pytest
```

## Runbook — the real-SDK demo (manual; never CI)

The gated/CI provider is the **deterministic fake** (`decisions.md`
`deterministic-fake-v1-provider`). The **real Claude Agent SDK body is the same
shape, swapped in** — `ClaudeAgentProvider` runs the headless `claude` CLI
inside the jail via `launch_confined` — and is run manually, with the
maintainer: nondeterministic, auth-needing, never a CI gate.

Prerequisites: macOS with `/usr/bin/sandbox-exec` (Seatbelt × clonefile — the
reversible × jailed cell of the run-mode matrix), the `claude` CLI on `PATH`
(probed at 2.1.172), and `ANTHROPIC_API_KEY` exported. Then:

```bash
uv run --package shepherd-dialect python spikes/260610-real-sdk-demo/run_demo.py
```

What it shows (budget ≈ 2 min total; the script SKIPs without the key):

- **success ×2** — real Claude's Write tool creates a real file in the carrier's
  working copy inside the jail; the delta is captured implicitly at merge; the
  durable hybrid trace reads back `merged`, and the fourth-row `task.invocation`
  digest recomputes byte-exactly and **holds across both runs** (cross-run
  identity under `shepherd.kernel.canonical.v2`, real body).
- **readonly** — `may=ReadOnly` refuses fail-closed at the jail. The CLI *hangs*
  under the denied network (S1 finding: `spikes/260610-real-sdk-jail-probe`), so
  the provider's argv carries its own hard stop (perl `alarm`+`exec`, mandatory —
  `launch_confined` has no timeout); the wrap discards; ground stays pristine;
  the trace outlives the discard (`discarded`, output pointer `None`).
- **supervised-deny** — `drafts_only_supervisor` (Pattern B, check-at-commit)
  denies the real agent's out-of-`drafts/` delta at the last undo point; the
  denial is recorded into the durable trace as a `supervisor.decision` event.

Honest non-claims (execution-boundary.md §7): no network `may=` enforcement
claim (coarse all-or-nothing until the egress broker); the CLI's own
`--allowed-tools` gating is the *advisory framework tier* — the jail is the
boundary; command-lane effects from inside the jail are Phase E.

Evidence of the maintainer-run demo: `spikes/260610-real-sdk-demo/FINDINGS.md`.
Plan: [`260610-1727-real-sdk-demo-plan.md`](../../../260610-1727-real-sdk-demo-plan.md).

## The Codex lane — managed ChatGPT subscription auth (preview)

`CodexAgentProvider` is a publishable, optional Python provider backed by the
exact `openai-codex==0.144.4` SDK and its bundled app-server. Install the
`codex` extra, create a Shepherd-owned profile, and perform the no-model
readiness probe before running:

```bash
pip install 'shepherd-dialect[codex]'
shepherd codex login --profile default --mode chatgpt
shepherd codex status --profile default --probe
shepherd doctor codex --profile default --probe
```

An existing Codex CLI login is never imported implicitly. Opt into a link
without copying token bytes with `shepherd codex adopt --profile default`, and
remove only Shepherd's selected profile with `shepherd codex logout`. API-key
profiles are optional (`--mode api-key`); the key is read through a hidden
prompt and passed directly to app-server login, never through task arguments or
durable evidence. Provider startup refuses profile roots and resolved auth
symlink targets that overlap the run workspace.

The authenticated broker runs outside the outer task jail because it must read
and refresh account state. Before sending a prompt it proves an
invocation-specific Codex permission profile: model-selected tool children see
only the canonical workspace grants, cannot read broker/profile/runtime state,
and get the lowered deny/all/host-list network policy. The parent environment is
scrubbed and pre-prompt canaries prove it contains no credential-bearing fields,
even on Linux runtimes where a child can inspect parent `/proc`. `approvalPolicy=never`
is always sent; unexpected approval requests are
captured and explicitly declined. This is provider tool-sandbox enforcement,
not bypass mode.

Every inbound app-server line is captured before SDK parsing/routing as one
redaction-safe, hash-chained `ProviderActivity`; unknown and malformed frames
are counted too. Success requires a verified terminal manifest. Recognized
activities additionally project into the same `ProviderEvent` vocabulary used
by other providers. Token usage is retained when app-server reports it.
ChatGPT runs retain before/after subscription-credit balances when available,
but never invent a currency cost; API-key runs likewise report no dollar amount
unless the upstream protocol eventually supplies one. Completed native file
claims are provider attestations only and are reconciled against the carrier
tree as `carrier_confirmed`, `provider_only`, or `carrier_only`.

Workspace-control selects this provider through the existing runtime seam:

```python
runtime = {
    "provider": {"id": "codex", "profile": "default", "mode": "chatgpt"},
    "model": "gpt-5.4",
}
```

The lane requires native jail placement for the reversible carrier while its
tool policy is enforced by Codex's own sandbox. A hard deadline first requests
`turn/interrupt`, then the supervisor terminates and reaps the entire broker
process group if graceful shutdown does not complete. See
[`shepherd/docs/guides/codex-provider.md`](../../docs/guides/codex-provider.md)
for the standalone runbook and evidence model.

### Triage — `confined body refused`

The Claude CLI providers now name the cause in the raised error; this is the
map from what you see to what to do:

- **`rc=1`** — the CLI errored and reported it inside its result envelope; the
  message carries the CLI's own `result` text and a remedy. `auth_missing` /
  `auth_expired` mean no usable jailed login (set `CLAUDE_CODE_OAUTH_TOKEN` from
  `claude setup-token`, or `ANTHROPIC_API_KEY`); `access_denied` (HTTP 403) is an
  account/org policy limit, **not** a login problem (different key or org admin);
  `root_permission` is the rootful `--dangerously-skip-permissions` refusal.
- **`rc=-14`** — the `budget_seconds` alarm fired (`BudgetExhausted`). With
  streamed output the model genuinely ran long; with **zero** output the CLI
  likely hung before starting (a stale `claude` version or a blocked network).
- **Rootful hosts (containers/CI):** the CLI refuses bypass permissions as root.
  Set `IS_SANDBOX=1` **only** when you are intentionally in a sandbox/container,
  or run as a non-root user.
- **Wrappers that authenticate out-of-band:** a keyless jailed run is refused
  before launch; set `SHEPHERD_ALLOW_KEYLESS_CLAUDE=1` to launch anyway, and pair
  it with `SHEPHERD_NO_CREDENTIAL_SEEDING=1` if a stale standard credential would
  otherwise be seeded ahead of the wrapper's real auth.

## The hermes lane — the multi-model provider (manual; never CI)

`HermesHeadlessProvider` drives the [hermes-agent](https://pypi.org/project/hermes-agent/)
CLI (Nous Research) in oneshot mode inside the same jail contract as the claude
lanes — and is the first lane that routes **non-Anthropic models**: construct it
with `model=` and `model_provider=` (both required; `anthropic`, `openai`, or
`openrouter` — the v1 auth set, resolved via env keys: `ANTHROPIC_API_KEY` /
`OPENAI_API_KEY` / `OPENROUTER_API_KEY`). A scrubbed `HERMES_HOME` has no
account default: the seeded config *is* the model selection. Spiked and
reviewed at `HERMES_TESTED_VERSION` (0.18.2); `shepherd doctor hermes
--provider <p>` is the readiness surface, `--probe --model <id>` the
authoritative auth check, and the version pin warns (never gates) on drift.

**The envelope is the outcome authority, not the exit code** — hermes exits 0
on failure. Success means the `--usage-file` envelope says `completed: true`
(absence-of-`failed` is *not* success: partial/interrupted runs report
`completed:false, failed:false` at rc 0). The failure vocabulary a trace
reader will meet, in check order: `ScratchScrubResidue` (the D3 scrub left
residue — fail-closed, the scratch holds the unredacted transcript),
`BudgetExhausted` (rc −14, the alarm; the partial transcript rides the
exception's events channel), `ConfinedProcessRefused` (rc≠0),
`UsageEnvelopeMissing` (rc 0, no envelope — contract violated),
`EnvelopeReportedFailure` (`failed: true`; diagnosis prefers the envelope's
structured `failure` key over reply text), `EnvelopeNotCompleted`
(`completed != true`). Event evidence (tool calls, usage, cost — the richest
of any lane) is harvested from the scratch `state.db` before the scrub.

**Deliberately off, and load-bearing:** the `-t file,terminal` toolset pin
disarms hermes's learning loop (skills/memory background review), default-on
`delegate_task` fan-out, and the browser sidecar; the seeded
`compression: {enabled: false}` disarms the auto-compression aux LLM call the
toolset gate cannot reach; no `--checkpoints`/`-w` (VcsCore owns
reversibility). `SHEPHERD_ALLOW_KEYLESS_HERMES=1` is the wrapper escape hatch.

**Known exposure (execplan §4.6):** the alarm kills the provider process, not
its tree — a terminal-tool child survives as a live process until the
jail-level reap lands. The S3 evidence run narrowed the blast radius: under
the fuse carrier + Landlock a survivor's post-merge writes land in the
discarded working copy, never the ground (asserted in the evidence suite);
the live exposure is the surviving process plus the pre-merge capture window.

Jailed evidence (Landlock × fuse; needs the CLI + key; the §4.6 canary is a
strict xfail until the reap lands):

```bash
uv run pytest shepherd/packages/dialect/tests/test_jailed_hermes_run_linux.py -m container
```

## The authoring surface (re-pinned 2026-06-10)

Function-form only (triage D1): `@task` bodies are plain functions; the
class-form API retired with the spine.

- **Checks** — `Annotated[str, NonEmpty()]` on parameters (preconditions —
  refused *before* the reversible fork: no carrier cost, durable trace terminal
  `refused`) and on the return annotation (postconditions — the wrap discards).
  Builtins: `NonEmpty`, `InRange`, `Matches`, `MaxLength`, `FileExists`.
- **`@step`** — the docstring is the model prompt; outputs parse into the
  declared return type; `step.{started,completed,failed}` land in the run's
  durable trace (no parallel stream).
- **Metadata & serde** — `extract_task_metadata`, `task_input_model`,
  `dump_task_args`/`load_task_args` (the JSON-boundary roundtrip; also the
  typed fourth-row args key — same values ⇒ same cross-run digest),
  `task_prompt`, `extract_task_source`.
- **Source validation** — `validate_task_source`/`check_task_source`: the
  dependency-free **advisory** filter; the jail is the enforcement boundary
  (ledger `source-validation-is-advisory-the-jail-enforces`).
- **Autoconfig (mechanical)** — `Infer`, `extract_infer_fields`,
  `build_inference_model`; the LLM half rides the battery tranche.
