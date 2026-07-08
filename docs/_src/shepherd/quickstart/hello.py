"""Quickstart example (tested in CI against the simulated offline provider)."""

# --8<-- [start:hello]
import shepherd as sp


# A task is a typed contract: signature + docstring. These two are pure —
# they take values and return values. (Tasks that touch a repository declare
# it in their signature and run through a workspace — see the homepage hero.)
@sp.task
def implement(spec: str, feature: str) -> str:
    """Write an implementation plan for the feature against this spec."""


@sp.task
def review(plan: str, feature: str) -> str:
    """Review the plan for the feature. Name risks and missing tests, then conclude."""


with sp.workspace(model="claude:sonnet-4-5"):
    plan = implement(spec="users sign in with email + password", feature="login")
    print(review(plan=plan, feature="login"))
# --8<-- [end:hello]
