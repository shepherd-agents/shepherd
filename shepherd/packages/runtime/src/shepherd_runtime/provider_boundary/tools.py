"""D3 ``ToolHandler`` interface.

The tool-call interposition boundary: the recorder uses
``ToolHandler.lookup`` to resolve ``tool.<name>`` handlers installed
in the active binding-env chain. Two-step boundary — lookup returns
an entry (or ``None``); the recorder invokes ``entry.invoke(payload)``
to run the handler.

Owner per CONTRACTS D3: Plan 04 (effects-nucleus). The Protocol +
stub live in ``provider_boundary`` as a transient location until
production handler dispatch absorbs them in Tranche 7+.

Pinned by `docs/design/proposed/260505-plans/CONTRACTS.md` D3.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from shepherd_runtime.trace import Ref

__all__ = ["StubToolHandler", "ToolHandler", "ToolHandlerEntry"]


@dataclass(frozen=True)
class ToolHandlerEntry:
    """Resolved entry for a ``tool.<name>`` effect kind.

    ``handler_id`` populates ``HandlerSelection.handler_id`` in the
    emitted kernel record. Convention: ``{namespace}.{name}.v{version}``
    (e.g. ``local.read_file.v1``). The contract does not enforce the
    convention; consumers must not parse ``handler_id``.

    ``invoke`` is async; sync handler bodies are auto-wrapped per
    DECISIONS D6 sync/async dispatch.
    """

    handler_id: str
    handler_frame_ref: Ref
    invoke: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class ToolHandler(Protocol):
    """Lookup helper for ``tool.<name>`` handlers in the active binding-env chain.

    The implementation walks the binding-env chain per CONTRACTS C6:

    1. Active Scope (innermost wins)
    2. Workspace root Scope
    3. Driver registry (deferred; v1 returns ``None`` for tier 3)

    Per-class ``on_unhandled`` (tier 4) is the recorder's concern,
    not the lookup helper's. Returning ``None`` is the explicit
    not-found signal; the recorder raises ``ToolHandlerNotFoundError``
    when it receives ``None`` for a tool effect kind that the SDK
    invoked.
    """

    def lookup(self, effect_kind: str) -> ToolHandlerEntry | None: ...


class StubToolHandler:
    """In-memory ``ToolHandler`` stub for consumer tests.

    Tests register concrete callables via ``install``; ``lookup``
    returns a ``ToolHandlerEntry`` that wraps the callable. The stub
    does not consult any binding-env chain — it's a flat dict.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, ToolHandlerEntry] = {}

    def install(
        self,
        effect_kind: str,
        fn: Callable[[dict[str, Any]], Any],
        *,
        handler_id: str | None = None,
    ) -> None:
        async def _async_invoke(payload: dict[str, Any]) -> dict[str, Any]:
            result = fn(payload)
            if inspect.iscoroutine(result):
                result = await result
            return result

        # Effect kind is "namespace.name"; derive the bare name for the id.
        bare = effect_kind.split(".", 1)[1] if "." in effect_kind else effect_kind
        self._handlers[effect_kind] = ToolHandlerEntry(
            handler_id=handler_id or f"local.{bare}.v1",
            handler_frame_ref=f"frame:{effect_kind}",
            invoke=_async_invoke,
        )

    def lookup(self, effect_kind: str) -> ToolHandlerEntry | None:
        return self._handlers.get(effect_kind)
