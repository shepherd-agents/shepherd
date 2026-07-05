# Placements

> Page status: release-ready
> Source state: shipped-source
> Applies to: Shepherd v0.2.0
> Owner: @docs-system-owner (TBD)
> Validation: shepherd/packages/dialect/tests/test_workspace_control_workstream3.py

*Concept. The mental model behind Shepherd. Steps live in the tutorial, signatures in the reference.*

A **placement** decides *where* a run's body executes, and therefore whether its
[permission grants](permissions.md) are enforced by the operating system or only recorded as
advisory. It is a per-run choice on `workspace.run`:

```python
run = workspace.run(task, bindings={...}, placement="jail")
```

## The three placements

- **`"jail"`** — run the body in the native syscall jail (macOS Seatbelt; Linux Landlock). The
  run's writable roots are compiled from its grants and enforced by the OS: a write outside a
  `ReadWrite`-granted root is refused at the syscall. `jail` **fails closed** — if the host
  cannot establish a monitor, the run refuses rather than silently downgrading.
- **`"advisory"`** — run the body in-process without a jail. Grants are **recorded but not
  enforced by the OS**; the run's `enforcement` reads `advisory`, honestly, so a reader can never
  mistake it for jailed. Useful as a fast dev lane on hosts without a jail.
- **`"auto"`** (default) — use the native jail on a jail-capable host, and record advisory
  execution otherwise. `auto` never fails closed: it degrades to advisory *visibly*.

The enforcement claim in [Permissions](permissions.md) — "refused at the syscall" — holds on a
**jailed** placement. On `advisory`, the same grant is a recorded intention, not an OS-enforced
boundary. The run record carries which placement resolved, so the distinction is always legible
after the fact.

!!! note "Scope (0.2.0)"
    Native jail enforcement is exercised on macOS Seatbelt; Linux Landlock is container-gated.
    Placement selects the execution boundary for the workspace/Git substrate; remote and cloud
    devices are out of this cut.
