"""SIMULATION SHIM — not the real product.

Stands in for the unshipped ``shepherd`` package (surface) so the
prototype's documented examples run end-to-end, deterministically, offline.
It faithfully mirrors the load-bearing semantics the docs teach:

- ``@shp.task`` on a bodyless function REQUIRES a docstring (the docstring is
  the model-call goal — same contract as
  shepherd_runtime/nucleus/callable_task.py:272-278);
- calls are answered from recorded transcripts (docs_src/_sim/transcripts.json),
  the prototype's stand-in for the deterministic offline provider;
- results are coerced to the declared return type (dataclass or str).

When the real ``shepherd`` facade ships, examples switch from this shim to the
product by *removing* the sys.path entry in docs_src/conftest.py — the example
code itself does not change. That is the migration contract being prototyped.
"""

from __future__ import annotations

import dataclasses
import functools
import inspect
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, get_type_hints

_TRANSCRIPTS = json.loads(
    (Path(__file__).resolve().parent.parent / "transcripts.json").read_text(encoding="utf-8")
)

_ACTIVE: dict[str, Any] = {"model": None}


# --- permission surface (simulation) -------------------------------------
# Inert stand-ins so signature-level grants — ``repo: May[GitRepo, ReadWrite]``
# — parse and type-check like the shipped surface. The simulation never
# inspects them; the real facade enforces them at the native syscall jail.
class GitRepo:
    """Inert substrate-handle type (simulation)."""


class ReadOnly:
    """Inert read-only grant profile (simulation)."""


class ReadWrite:
    """Inert read-write grant profile (simulation)."""


class May:
    """Per-parameter grant marker (simulation): ``May[GitRepo, ReadWrite]``."""

    def __class_getitem__(cls, _params: Any) -> type["May"]:
        return cls


class DeliveryFailed(RuntimeError):
    """Raised when a recorded answer cannot be coerced to the return type."""


def _coerce(value: Any, annotation: Any) -> Any:
    if annotation is inspect.Signature.empty or annotation is None:
        return value
    if dataclasses.is_dataclass(annotation) and isinstance(value, dict):
        fields = {f.name for f in dataclasses.fields(annotation)}
        missing = fields - set(value)
        if missing:
            raise DeliveryFailed(f"transcript missing fields {sorted(missing)} for {annotation.__name__}")
        return annotation(**{k: v for k, v in value.items() if k in fields})
    if annotation is str and not isinstance(value, str):
        raise DeliveryFailed(f"expected str transcript, got {type(value).__name__}")
    return value


def task(fn=None, *, may=None, name=None, guidance=None):
    """Declare a typed task (simulation). Mirrors the bodyless-docstring rule."""

    def _wrap(target):
        doc = inspect.getdoc(target)
        if not (doc or guidance):
            raise TypeError(
                f"Bodyless callable task {target.__qualname__} must declare a docstring "
                "or guidance= to use as the model-call goal"
            )
        hints = get_type_hints(target)
        ret = hints.get("return")

        @functools.wraps(target)
        def _call(*args, **kwargs):
            if _ACTIVE["model"] is None:
                raise RuntimeError("call tasks inside `with shp.workspace(model=...)`")
            try:
                recorded = _TRANSCRIPTS[target.__name__]
            except KeyError as exc:
                raise DeliveryFailed(f"no recorded transcript for task {target.__name__!r}") from exc
            return _coerce(recorded, ret)

        _call.__shepherd_sim_task__ = True
        return _call

    return _wrap(fn) if callable(fn) else _wrap


@contextmanager
def workspace(*, model: object, root: str | None = None):
    """Open the ambient context (simulation: records the active model)."""
    _ACTIVE["model"] = model
    try:
        yield _ACTIVE
    finally:
        _ACTIVE["model"] = None


def deliver(value: Any) -> Any:
    """Complete a task with a typed delivery (simulation: identity)."""
    return value
