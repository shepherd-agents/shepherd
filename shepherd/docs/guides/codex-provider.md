# Headless Codex provider

The Codex runtime is a preview provider for Shepherd workspace runs. It uses
the pinned Python `openai-codex==0.144.4` package and bundled app-server; Node
and a globally installed `codex` executable are not runtime dependencies.

## Install and authenticate

Install the optional dependency and create a named ChatGPT subscription
profile:

```bash
pip install 'shepherd-ai[codex]'
shepherd codex login --profile default --mode chatgpt
shepherd codex status --profile default --probe
shepherd doctor codex --profile default --probe
```

`login` uses Codex's device-code flow. The profile lives outside every
workspace under the platform state directory, is serialized by a profile lock,
and is never copied into task arguments, run metadata, or the carrier tree.
Provider startup refuses a configured profile root or resolved auth symlink
target that overlaps the run workspace.
Provider execution does not inspect `~/.codex` implicitly. To reuse an existing
CLI login explicitly, link it without copying credential bytes:

```bash
shepherd codex adopt --profile default
```

Optional API-key profiles are supported without making them the default:

```bash
shepherd codex login --profile api --mode api-key
```

The CLI prompts with input hidden and calls the SDK login method in-process.
Do not place keys in runtime dictionaries or environment-backed task inputs.
`shepherd codex logout --profile NAME` deletes only that managed profile; an
explicitly linked source login remains intact.

## Run through workspace-control

Use the same public runtime option shape as the other built-in providers:

```python
run = task.run(
    repo=repo,
    args={"output_path": "result.md"},
    placement="auto",
    runtime={
        "provider": {
            "id": "codex",
            "profile": "default",
            "mode": "chatgpt",
        },
        "model": "gpt-5.4",
    },
)
```

Codex uses the existing execution-provider facade and workspace-control's
private runtime transport seam. It does not define a third public provider
interface. Native jail placement is still required for the reversible carrier.
The authenticated broker itself runs outside that outer jail so it can refresh
the account and establish Codex's nested tool sandbox in the correct order.

## Permissions and lifecycle

Shepherd generates a fresh Codex permission profile from the run's canonical
writable roots and network policy. Before sending the model prompt, no-model
canaries prove authorized workspace access, outside-write refusal, profile and
runtime denial, and absence of credential material from the scrubbed parent
environment (including through Linux `/proc`). Provider-selected
commands run with this profile. `approvalPolicy=never` is mandatory; an
unexpected approval request is first captured and then explicitly declined.

At the provider deadline the broker records a control activity, requests
`turn/interrupt`, waits briefly for the app-server terminal, and fails the run.
The outer hard deadline terminates the complete process group if any component
wedges. Runtime directories are private, per invocation, and removed after the
process is reaped.

## Evidence, tokens, and cost

The app-server stdout reader accounts for every line before JSON decoding or
SDK routing. Each line becomes a compact `ProviderActivity` with sequence,
byte length, raw SHA-256 digest, safe native summary, and a hash-chain link.
Unknown, error, request, response, delta, malformed, and terminal frames all
remain distinguishable. A successful invocation is impossible until the
controller verifies the final count, chain, terminal, manifest, process exit,
and absence of post-terminal records.

Recognized native items also project monotonically to Shepherd's standard
`ProviderEvent` records. Raw prompts, model text, commands, output, diffs, MCP
arguments/results, and credentials are not retained in activities. Final model
output remains available only in the normal provider result.

When `thread/tokenUsage/updated` is present, its input, cached input, output,
reasoning, total, and context-window values are retained. For ChatGPT profiles,
the broker reads rate/credit state before and after the turn. If balances are
available it reports subscription credits consumed; it leaves currency and
monetary amount null. API-key mode identifies API billing but likewise does not
guess a dollar amount.

A native `fileChange` is not filesystem authority. After the app-server closes,
its canonical relative paths are compared with an independently hashed carrier
tree delta and labeled `carrier_confirmed`, `provider_only`, or `carrier_only`.
Only VcsCore's carrier capture creates authoritative persisted file effects.

## Upgrade and troubleshooting

The SDK/runtime pair is fail-closed at `0.144.4`. A version mismatch requires a
re-audit of protocol schemas, authentication storage, permission profiles,
event coverage, and the bundled runtime. Start troubleshooting with:

```bash
shepherd codex status --profile default --probe
shepherd doctor codex --profile default --probe --json
```

The probe reads/refreshes account state but does not call a model. A failed
native-jail check means workspace-control cannot start the lane on that host;
it is separate from the Codex tool-sandbox proof.
