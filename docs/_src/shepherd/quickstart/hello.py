"""Quickstart example (tested in CI against the simulated offline provider)."""

# --8<-- [start:hello]
import shepherd as shp
from shepherd.providers import claude


@shp.task
def implement(repo: str, feature: str) -> str:
    """Implement the feature in the repo and report what changed."""


@shp.task
def oversee(worker, repo: str, feature: str) -> str:
    """Run the worker on the feature. If its tests fail, revert and retry, then report."""


with shp.workspace(model=claude("sonnet-4-5")):
    print(oversee(implement, repo=".", feature="login"))
# --8<-- [end:hello]
