"""Internal feature-flag scopes used by workspace-control retained-output paths."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping


@contextmanager
def scoped_seal_and_select() -> Any:
    """Enable the seal-and-select lane for the duration of the ``with`` block.

    Public: exported from the ``shepherd_dialect`` facade so hosts (the CLI)
    scope the flag around a single invocation instead of mutating ambient
    process env at import/entry — the latter leaks across in-process
    ``CliRunner`` calls and poisons test order (W1c).
    """
    with _env_enabled({"VCS_CORE_SEAL_AND_SELECT": "1"}):
        yield


# In-package alias: the retained-output paths and tests already scope the flag
# correctly via this name; keep it so this change stays a rename of the public
# entry, not a 40-site churn.
_seal_and_select_enabled = scoped_seal_and_select


@contextmanager
def _env_enabled(required: Mapping[str, str]) -> Any:
    old_values = {name: os.environ.get(name) for name in required}
    os.environ.update(required)
    try:
        yield
    finally:
        for name, old_value in old_values.items():
            if old_value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old_value


#: The env flag that gates the seal-and-select settlement lane. Kept in sync
#: with vcs-core's ``SEAL_AND_SELECT_ENV`` by value (a durable-token agreement,
#: not a private import — the d2 boundary forbids reaching into ``vcs_core._*``).
_SEAL_AND_SELECT_ENV = "VCS_CORE_SEAL_AND_SELECT"


def effective_feature_flags() -> dict[str, bool]:
    """The resolved state of the flags that alter durable run behavior.

    Provenance for the run record (P1.2 / finding #5): a flag that changes what
    a run durably does — today only ``seal_and_select`` (whether ``retained``
    scopes are a legitimate settlement lane) — must be recorded, so two runs
    under different flag state are distinguishable in the durable evidence.
    Reads the same env the lanes consult, at run-record-build time.
    """
    value = os.environ.get(_SEAL_AND_SELECT_ENV, "").strip().lower()
    return {"seal_and_select": value in {"1", "true", "yes", "on"}}
