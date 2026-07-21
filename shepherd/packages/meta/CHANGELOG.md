# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`shepherd doctor hermes` — readiness checks for the hermes multi-model
  runtime lane.** Native jail, `hermes` CLI on PATH, a warn-only version pin
  against the tested hermes-agent release (drift names the re-audit list
  without gating), and offline env-key auth resolution for an explicit
  `--provider` (`anthropic`, `openai`, or `openrouter` — the lane has no
  account default). `--probe --model <id> --provider <p>` performs the
  authoritative network-reaching auth check under the provider's
  scrubbed-home + seeded-config conditions.
- **`shepherd doctor` rejects `--json`/`--backend` placed before a
  subcommand** instead of parsing and silently dropping them.

## [0.3.0] - 2026-07-08

### Added

- **`apply` completes the retained-output settlement vocabulary.** `apply` joins
  `select` / `release` / `discard` as the fourth verb: where `select` is
  fast-forward-only (it fails closed if the workspace moved on since the run's fork
  basis), `apply` three-way-merges a run's whole delta onto the advanced workspace
  when the two change sets are path-disjoint, and fails closed on any overlap — no
  content synthesis at the settlement boundary. `workspace.apply(output)` /
  `RunOutput.apply()` / `shepherd run apply <exact-run-ref>`. This is whole-output
  apply; per-binding and sub-root apply remain deferred. Seal-mode is now
  unconditional (the `VCS_CORE_SEAL_AND_SELECT` flag is removed), and Linux Landlock
  grant enforcement is now executed (retiring the 0.2.0 container-gated caveat).
  *Note:* an in-session `select` → re-fork → run loop can misreport the second run's
  changeset and phantom-refuse `apply`; fork all candidates before the first
  settlement, or use one settlement chain per workspace session (ISS-013, fix
  post-0.3.0).
- **`ws.tasks.register(fn)` and `ws.run(fn, ...)` accept the task object directly.**
  A task is spelled once — `@sp.task` with a signature and docstring — and registered
  or run by passing the function, with no source-text blob. Decorated tasks register
  (and their bodies execute under the jail as plain functions); a task defined in an
  importable module travels as that module; a bodyless task written in a run-as-script
  file (`__main__`) is captured at definition scope. `ws.run(fn, ...)` and
  `ws.tasks.task(fn)` resolve the callable by the default callable-identity
  convention; a callable registered with an explicit `task_id=` must still be run by
  that id. The
  task-level `may` ceiling is derived from the signature's grants — uniformly, however
  the task was registered; an explicit `may_default=` still overrides, and the registry
  records which one happened.
- **Registrations record the `GitRepo` grant spelling.** Each registration records
  whether a `GitRepo` grant was written bare (`repo: GitRepo`) or explicit
  (`May[GitRepo, ...]`) as registration provenance (`gitrepo_grant_spelling` in the
  signature schema). The two compile to a byte-identical grant, so this is the only
  discriminator; it feeds the future no-defaulted-grants lint. No behavior change, no
  effect on grant identity, enforcement, or the content-addressed task artifact.

### Changed

- **The beginner workspace-handle spelling is now `repo: GitRepo`.** A bare
  `GitRepo` parameter in a task signature is an explicit writable workspace-handle
  grant, equivalent to the read-write case of `May[GitRepo, ...]`; it is not
  inferred from the parameter name. Use `May[GitRepo, ReadOnly]` when the handle
  must be read-only. Pre-0.3 unannotated `repo` parameters are no longer treated
  as handles; annotate them or pass an ordinary value through `args={"repo": ...}`
  when `repo` is truly a value argument.
- **A bodied task registered from a run-as-script file (`__main__`) now refuses.**
  Previously its whole script was captured as the task artifact (embedding driver
  code — a re-execution footgun); it now refuses with a remedy ("move it to an
  importable module such as `tasks.py`"). Bodyless script tasks are captured at
  definition scope instead. An unregistered task callable passed to `ws.run(...)`
  refuses with a register-it-first hint rather than silently auto-registering.

- **Ambient calls of handle-declaring bodyless tasks now refuse loudly.** A bodyless
  task whose signature carries substrate-handle annotations (`May[GitRepo, ...]`
  parameters or handle-typed returns) raises `AmbientWorldAccessRefused` at every
  ambient spelling (`task(...)`, `task.run(...)`, `task.detailed(...)`) — before any
  handler or provider dispatch, keyed on the annotation, never the passed value:
  *"task ... declares world access in its signature ...; a bodyless ambient call
  cannot honor it. Run it through retained execution instead: workspace.run(...)"*.
  Previously the grant was silently erased into prompt evidence and a reachable
  `model.call` handler returned a fabricated typed result claiming repo work no
  in-process model call can have done. Pure-value bodyless tasks and bodied Python
  tasks are unaffected.
- **Handle-typed return slots refuse at schema generation (the fabrication fence).**
  Provider-facing output schemas are never generated for substrate-handle return
  types (`GitRepo`, including inside tuples/`Annotated`): both schema stacks raise
  `HandleReturnSlotUnsupported` with an identical message instead of emitting an
  object schema a model could fill with a fabricated custody claim. Returned handles
  arrive with the projector (P-030 phase iii); until then, return ordinary values and
  consume world output through `RunOutput`/`Changeset` settlement. Task-registration
  *parameter* schemas are unaffected.
- **Unclassifiable delegating task bodies raise loud instead of returning `None`.**
  An exec/REPL/notebook-defined task whose source is unavailable and whose compiled
  body is empty-shaped raises `AmbiguousTaskBody` at call time (with the importable-
  `.py`-file remedy) rather than silently delivering `None`. File-defined bodyless
  tasks and non-trivial bodies classify and run as before.

### Fixed

- **The documented mock idiom now intercepts model calls.** The taught
  `handle("model.call.requested", ...)` spelling resolves (installation-time dual-key
  shim in both nuclei); previously only the bare `"model.call"` key dispatched, so
  documented mocks were silently ignored — and a reachable provider would take the
  call instead. The *recorded* effect-kind string stays `"model.call"` (durable
  vocabulary; the kind-string bump is deliberately not taken here).
- **Notebook cell re-runs no longer trap the session.** Re-configuring
  `workspace(...)` while idle tears down and replaces the active workspace instead of
  raising `WorkspaceAlreadyConfigured`; reconfiguration during an active task run
  still refuses.

## [0.2.1] - 2026-07-06

Auth-lane hardening for the jailed `claude` provider (batches 2a–2d), published to
the public mirror as `v0.2.1`.

### Fixed

- **The jailed `claude` CLI lane now fails a doomed run *before* launch.** When no
  usable credential resolves (no `ANTHROPIC_API_KEY`/`CLAUDE_CODE_OAUTH_TOKEN`, no
  seedable host login) or a seeded subscription login is expired, the public
  headless provider refuses at preflight (`auth_missing` / `auth_expired`,
  `launch_attempted: false`) instead of spending a confined launch that reads like
  a jail denial. Set `SHEPHERD_ALLOW_KEYLESS_CLAUDE=1` to opt a wrapper that
  authenticates out-of-band back into the launch path.
- **Actionable CLI failure diagnosis.** A nonzero `claude` exit now surfaces the
  CLI's own reason plus a remedy (not a blind 300-char tail): not-logged-in,
  org-policy `access_denied` (HTTP 403), and rootful `root_permission` are each
  classified with the safe envelope scalars recorded in the trace. A
  `budget_seconds` alarm kill (`rc=-14`) maps to `BudgetExhausted`, with a
  hung-body hint when the CLI produced no output at all.

### Added

- **`shepherd doctor claude --probe`** performs a real auth round-trip (in the
  parent, under the provider's scrubbed-config/seeding conditions — not through
  the jail); the offline `claude-auth` check hard-fails an expired subscription
  token rather than reporting a merely readable blob as ready.

## [0.2.0] - 2026-07-05

### Added

- **Per-binding signature grants, enforced at the OS.** A task can declare a
  read-only or read-write grant per bound `GitRepo`/`Folder` directly in its
  signature — `docs: May[GitRepo, ReadOnly]`, `backend: May[GitRepo, ReadWrite]`.
  On a jailed device the grant is compiled to that run's writable roots and
  enforced at the native syscall jail (macOS Seatbelt; Linux Landlock): a write
  to a `ReadOnly`-granted root, or to any managed path not covered by a
  `ReadWrite` grant, is refused at the syscall — before the last undo point, not
  advised and not caught only at a merge gate.
- **Named multi-binding acquisition.** `ws.bind(root="backend/", name="backend")`
  returns the bound handle; `workspace.run(task, bindings={...})` runs a task
  against more than one named binding. Overlapping or nested binds are refused at
  bind time (the disjoint-roots invariant that keeps per-binding enforcement
  sound).
- **Per-binding changeset views.** `run.changeset(name="backend")` inspects one
  binding's world output; settlement stays consume-once via `select` / `release`
  / `discard`.
- **`shepherd task show`** renders the per-parameter grant surface expanded, so
  reading the signature is reading the permission surface.

### Notes

- Scope: per-binding whole-profile `ReadOnly`/`ReadWrite` over disjoint named
  bindings, jailed device, filesystem / Git substrate, same-process
  value-children. Enforcement is exercised on macOS Seatbelt; Linux Landlock is
  container-gated. Sub-root / `where(path=...)` grants and write-returning
  handles are not part of this cut.
- Internal package version pinning was unified: workspace-internal version
  floors were removed and the framework family versions aligned to the release.

## [2.0.0a1] - 2026-01-09

### Added

- **Sync-First API**: Tasks auto-execute on instantiation — no async/await needed for basic usage:
  - `shepherd.configure(provider=...)`: Global default provider configuration
  - `shepherd.effects`: Global effect stream access
  - `Scope()`: Nested scopes for grouping tasks and multi-provider workflows
  - `Task(input=x)`: Auto-executes on instantiation (sync by default)
  - `await Task.arun(...)`: Explicit async for parallel execution
  - Three progressive API levels from simple scripts to multi-provider workflows

- **Three-Layer Architecture**: New foundational design separating concerns into:
  - **Layer 1 (Scope)**: Resource container that owns context bindings and effect streams
  - **Layer 2 (ExecutionLifecycle)**: Orchestrates the 5-phase lifecycle (configure → prepare → execute → capture → cleanup)
  - **Layer 3 (Provider)**: Translates abstract configuration to SDK-specific calls

- **Multi-Provider Support**:
  - `ClaudeProvider` for Claude Agent SDK integration
  - `OpenAIProvider` for OpenAI Agents SDK integration
  - Abstract `Provider` base class for custom implementations

- **ProviderBinding Composition**: Declarative, provider-agnostic configuration with well-defined merge semantics:
  - Capabilities: intersection (most restrictive wins)
  - Blocked tools: union (all blocks apply)
  - Trust level: most restrictive wins
  - Session isolation: most isolated wins

- **Rich Context Implementations**:
  - `WorkspaceRef`: Git-backed workspace with patch accumulation
  - `SessionState`: Conversation continuity with fork semantics
  - `BankingContext`: Financial operations (COMPENSABLE reversibility example)
  - `DatabaseContext`: Read-only SQL access (NONE reversibility example)
  - `AppStoreContext`: App Store Connect API integration
  - `MCPServerContext`: Zero-code MCP server configuration
  - `KVStoreContext`: Simple key-value store

- **Effect System**:
  - 25+ effect types covering task lifecycle, tools, files, sessions, and domain events
  - Attribution metadata (task_name, provider_id, context_id) on all effects
  - Immutable `Stream` with rich query API (`by_task()`, `by_context()`, `first()`, `last()`)
  - JSON serialization support

- **ExecutionContext Protocol**:
  - Clean separation of pure (`configure()`) vs imperative (`prepare()`/`capture()`/`cleanup()`) phases
  - `ExecutionContextDefaults` mixin for simple implementations
  - Reversibility levels: AUTO, COMPENSABLE, NONE with composition semantics

- **Task and Step Decorators**:
  - `@task` for declarative task definitions with Pydantic models
  - `@step` for LLM-powered methods within composite tasks
  - Field markers: `Input()`, `Output()`, `Context()`, `Artifact()`, `Check`

- **Artifact System**: File-based outputs written by LLM and read back after execution

- **Verbose Output**: Real-time console formatting with `VerboseConfig` and `VerboseFormatter`

- **GitHub Integration**: Domain-specific effects and tasks for GitHub operations

### Changed

- **Breaking**: Complete architectural redesign from v1.x
- Contexts are now provider-agnostic (express needs via `trust_level`, `session_isolation`, etc.)
- Effect stream is the single source of truth (replaces scattered state)
- Explicit lifecycle management replaces implicit auto-execution

### Migration from v1.x

The v2.0 release is a complete redesign with a simpler API. Key migration steps:

1. **Simplest path** — use the new sync-first API:
   ```python
   # v1.x
   result = await execute_task(MyTask, {"input": "value"})

   # v2.0 — just configure and instantiate!
   import shepherd
   shepherd.configure(provider=ClaudeProvider(name="default"))
   result = MyTask(input="value")  # Auto-executes
   ```

2. **For effect access** — use the global stream or a nested `Scope`:
   ```python
   # v2.0 — effects accessible via global stream
   result = MyTask(input="value")
   print(shepherd.effects)  # All effects from global scope

   # Or use a nested Scope for isolation
   with Scope() as scope:
       result = MyTask(input="value")
       print(scope.effects)  # Effects from this scope
   ```

3. **For multi-provider workflows** — use `Scope` with multiple providers:
   ```python
   # v2.0 with multiple providers
   with Scope() as scope:
       scope.register_provider("default", provider, default=True)
       workspace = scope.bind("workspace", WorkspaceRef.from_path("/repo"))
       result, outputs = await scope.execute(prompt)
       # workspace ContextRef auto-updates with changes
   ```

4. **Update context implementations** to use the new protocol:
   ```python
   # v2.0 context protocol
   class MyContext:
       def configure(self, capabilities) -> ProviderBinding: ...
       def prepare(self) -> Self: ...
       def capture(self, result) -> CaptureResult[Self]: ...
       def cleanup(self, error) -> None: ...
   ```

5. **Use ProviderBinding** for provider-agnostic configuration instead of SDK-specific options

See the [Migration Guide](https://github.com/anthropics/shepherd/blob/main/docs/migration.md) for detailed instructions.

## [1.x] - Previous Releases

See git history for v1.x changelog entries.
