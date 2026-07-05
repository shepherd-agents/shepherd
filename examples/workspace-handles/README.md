# Workspace Handle Examples

These examples show the current public workspace-handle floor as ordinary
Python programs.

```bash
uv run python examples/workspace-handles/best_of_n.py
uv run python examples/workspace-handles/retry_until_acceptable.py
```

By default each script creates a temporary vcs-core workspace, runs the example,
prints a JSON summary, and removes the workspace. Pass `--workspace PATH` to use
a specific workspace directory, and pass `--keep` to retain a generated
temporary workspace for inspection.

The demonstrated floor is intentionally narrow:

- acquire `workspace.git_repo()`;
- run through `workspace.tasks.task(...).run(repo=...)`;
- inspect `WorkspaceRun.output().changeset()` or retained files;
- inspect persisted authority evidence with `WorkspaceRun.authority()`,
  `RunOutput.run_authority()`, and `RunOutput.settlement_policy()`;
- settle `RunOutput` custody with `workspace.select(...)`,
  `workspace.release(...)`, or `workspace.discard(...)`;
- reacquire `workspace.git_repo()` after selection.

These scripts do not expose `best_of_n`, `gather`, public `May[...]`, generic
`apply`, public direct-authority launch, named bindings, durable child runtime,
or cross-substrate transitions.
