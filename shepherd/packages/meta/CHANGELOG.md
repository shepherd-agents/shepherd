# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
