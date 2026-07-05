"""Public facade for Shepherd — the ``sp`` surface.

``import shepherd as sp`` exposes the whole first-run user API in one place:

* the offline **syntax nucleus** (``task``, ``workspace``, ``handle``, and the
  model/effect/permission algebra), and
* the **substrate-handle surface** (``GitRepo``, ``May``, ``RunOutput``,
  ``Changeset``, settlement, ``Flow``, …).

The handle surface lives in ``shepherd_dialect.workspace_control``, which pulls
the ``vcs_core`` substrate engine. To keep the offline ``@task`` path light and
cross-platform, those symbols are **lazily** loaded on first attribute access
(PEP 562): ``import shepherd`` alone does not import ``vcs_core``.

Advanced runtime, provider, context, domain, export, and device APIs remain
available from their owner packages.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from shepherd_runtime.effects import (
    EffectNotPermitted,
    EffectSurfaceEmpty,
    EffectSurfaceTooWide,
    Match,
    OverbroadHandler,
    Plan,
    PlanNotExtractable,
    Subset,
    handle,
)
from shepherd_runtime.nucleus import (
    Artifact,
    DeliveryException,
    DeliveryFailed,
    GitRepo,
    GitRepoBasis,
    Run,
    RunInProgress,
    RunRef,
    Workspace,
    deliver,
    emit_artifact,
    task,
    workspace,
)
from shepherd_runtime.nucleus.profiles import Permissive
from shepherd_runtime.scope import current_binding

from shepherd._effect_facade import ask, tell

if TYPE_CHECKING:
    from pathlib import Path

    # shepherd_dialect ships no py.typed marker yet, so it reads as untyped to
    # consumers; the sp.* handle symbols are typed as Any until pass-2 typing
    # hardening adds the marker (which will make these re-exports precise).
    from shepherd_dialect.workspace_control import (  # type: ignore[import-untyped]
        Changeset,
        ChangesetStat,
        Flow,
        FlowControlClient,
        GitRepoGrant,
        May,
        ReadOnly,
        ReadWrite,
        RunOutput,
        ShepherdWorkspace,
        WorkspaceRun,
        WorkspaceTask,
    )

__version__ = "0.2.0"

# The handle surface, resolved lazily (PEP 562). Every name here lives in
# ``shepherd_dialect.workspace_control``; importing that module pulls ``vcs_core``
# and ``pygit2``, so we defer it until a handle symbol is actually touched. This
# is what lets the offline first-run ``@task`` path stay import-light and run on
# platforms where the substrate engine is unavailable.
_LAZY_MODULE = "shepherd_dialect.workspace_control"
_LAZY: frozenset[str] = frozenset(
    {
        "ShepherdWorkspace",
        "RunOutput",
        "WorkspaceRun",
        "WorkspaceTask",
        "Changeset",
        "ChangesetStat",
        "May",
        "ReadOnly",
        "ReadWrite",
        "GitRepoGrant",
        "Flow",
        "FlowControlClient",
    }
)


def open(  # noqa: A001
    cwd: str | Path = ".",
    *,
    activate: bool = True,
    backend: str | None = None,
) -> ShepherdWorkspace:
    """Open the vcs-core-backed workspace and its handle surface.

    Discovers the enclosing ``.vcscore`` workspace (run ``sp init`` first)
    and returns the substrate-backed:class:`ShepherdWorkspace`, wiring the run and
    ledger drivers internally so callers never touch them.

    ``backend`` is forwarded to:meth:`ShepherdWorkspace.discover`: the default
    ``None`` auto-selects a working carrier per platform (clonefile on macOS,
    kernel/FUSE overlay on Linux, portable copy carrier as the floor);
    ``"clonefile"``/``"fuse"``/``"kernel"``/``"copy"`` force one explicitly.

    This is deliberately distinct from:func:`workspace`, the offline,
    process-local context manager used for the first-run ``@task``: ``workspace``
    is the in-process sketch; ``open`` is the provenance-backed run surface.
    """
    from shepherd_dialect.workspace_control import ShepherdWorkspace

    return ShepherdWorkspace.discover(cwd, activate=activate, backend=backend)


def __getattr__(name: str) -> Any:
    """Resolve the lazy handle surface on first access (PEP 562)."""
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(_LAZY_MODULE), name)
    globals()[name] = value  # cache so subsequent access skips __getattr__
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY})


# Ordered by the teaching flow, not alphabetically.
__all__ = [  # noqa: RUF022
    "__version__",
    # offline syntax nucleus
    "workspace",
    "Workspace",
    "task",
    "deliver",
    "handle",
    "ask",
    "tell",
    "Permissive",
    "current_binding",
    # runs / delivery
    "Run",
    "RunRef",
    "RunInProgress",
    "DeliveryException",
    "DeliveryFailed",
    "emit_artifact",
    "Artifact",
    # effect / permission algebra
    "Match",
    "Plan",
    "Subset",
    "EffectNotPermitted",
    "EffectSurfaceEmpty",
    "EffectSurfaceTooWide",
    "OverbroadHandler",
    "PlanNotExtractable",
    # handle surface — GitRepo value noun is eager+light; the rest lazy
    "GitRepo",
    "GitRepoBasis",
    "open",
    "ShepherdWorkspace",
    "WorkspaceRun",
    "WorkspaceTask",
    "RunOutput",
    "Changeset",
    "ChangesetStat",
    "May",
    "ReadOnly",
    "ReadWrite",
    "GitRepoGrant",
    "Flow",
    "FlowControlClient",
]
