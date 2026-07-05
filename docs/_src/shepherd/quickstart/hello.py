"""Quickstart example (tested in CI against the simulated offline provider)."""

# --8<-- [start:hello]
import shepherd as sp


# The signature is the permission surface: the grant on `repo` is what lets the
# task write the bound repository (see "Permissions" in the concepts docs).
@sp.task
def implement(repo: sp.May[sp.GitRepo, sp.ReadWrite], feature: str) -> str:
    """Implement the feature in the repo and report what changed."""


@sp.task
def oversee(worker: object, repo: sp.May[sp.GitRepo, sp.ReadWrite], feature: str) -> str:
    """Run the worker on the feature. If its tests fail, revert and retry, then report."""


with sp.workspace(model="claude:sonnet-4-5"):
    print(oversee(implement, repo=".", feature="login"))
# --8<-- [end:hello]
