"""Cross-provider helpers shared by the dialect's execution providers."""

from __future__ import annotations

import sys
from collections.abc import Mapping  # noqa: TC003 — annotations must resolve at runtime (typing.get_type_hints)
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

# The perl form used on macOS (Seatbelt) and as the pre-§4.6 shape: arm an
# alarm, then exec the command so SIGALRM (rc -14) is the always-on hard stop.
_PERL_ALARM_EXEC = "alarm shift @ARGV; exec @ARGV or die qq{exec: $!}"
_REAPER_PATH = Path(__file__).with_name("_reaper.py")


def hard_stop_prefix(budget_seconds: int) -> list[str]:
    """The outermost argv block: a ``budget_seconds`` alarm that bounds the body.

    One builder for every jailed lane (previously five byte-identical perl
    copies). On **Linux** it is the tree-reaping supervisor (``_reaper.py``,
    §4.6): SIGALRM there kills the whole descendant tree, defeating the
    ``setsid`` escape an agent's terminal/Bash child uses to outlive a bare
    ``exec``. On **macOS** (and anywhere without ``/proc``) it stays the perl
    ``alarm``+``exec`` form — identical to the pre-§4.6 behavior, no unverified
    reaper. Both surface the stop as returncode ``-14``.
    """
    if sys.platform.startswith("linux"):
        return [sys.executable, str(_REAPER_PATH), str(budget_seconds)]
    return ["/usr/bin/perl", "-e", _PERL_ALARM_EXEC, str(budget_seconds)]


# The D3 scrub is fail-closed across every jailed lane: residue that survives
# the scrub would ride the captured delta into retained output, carrying
# confined-provider housekeeping — seeded credentials (the claude scratch) or
# the unredacted agent transcript (the hermes state.db). The error type is
# uniform so a trace reader meets one name regardless of which lane refused.
SCRATCH_RESIDUE_ERROR_TYPE = "ScratchScrubResidue"


def scratch_residue_message(scratch_name: str) -> str:
    """The uniform fail-closed message when a lane's scratch survives the scrub."""
    return (
        f"{scratch_name} scrub left residue in the working copy — refusing to hand the delta "
        "forward with confined-provider housekeeping (seeded credentials / agent transcript) still present"
    )


def _provider_prompt(
    provider_prompt: str,
    task_body: Callable[..., Any] | None,
    args: Mapping[str, Any],
    provider_name: str,
) -> str:
    if provider_prompt:
        return provider_prompt
    if task_body is None:
        raise ValueError(f"{provider_name} needs a prompt or task body")
    from shepherd_dialect.task_meta import task_prompt

    prompt_body = getattr(task_body, "__shepherd_prompt_body__", task_body)
    prompt_args = getattr(task_body, "__shepherd_prompt_args__", args)
    return task_prompt(prompt_body, dict(prompt_args))


def _invocation_id(provider_id: str, execution: Any) -> str:
    identity = getattr(execution, "identity", None)
    scope_instance_id = getattr(identity, "scope_instance_id", None)
    scope_name = getattr(identity, "scope_name", None)
    suffix = scope_instance_id or scope_name or "unknown"
    return f"{provider_id}:{suffix}"
