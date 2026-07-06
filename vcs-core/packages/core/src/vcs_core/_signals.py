"""Signal handling for run boundaries: make a killed run discard, not orphan.

Python's default ``SIGTERM`` disposition terminates the process *without* unwinding
the stack, so an operation-lifecycle ``__exit__`` never runs and the open operation
is left orphaned — the wedge behind ``OrphanedOperationsError``. ``SIGTERM`` is the
common non-interactive stop: ``kill <pid>``, ``docker stop``, systemd, Kubernetes,
and most CI cancellations all send it. ``SIGINT`` (Ctrl-C), by contrast, already
raises ``KeyboardInterrupt`` and unwinds cleanly.

``terminate_as_interrupt`` closes that gap by routing ``SIGTERM`` through the exact
same clean-discard path ``SIGINT`` takes. ``SIGKILL`` / OOM / power-loss remain
uncatchable and are covered by run-start auto-recovery, not prevention.
"""

from __future__ import annotations

import contextlib
import signal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextlib.contextmanager
def terminate_as_interrupt() -> Iterator[None]:
    """Route ``SIGTERM`` through ``KeyboardInterrupt`` for the wrapped run's duration.

    Usable as a context manager (``with terminate_as_interrupt():``) or as a
    decorator (``@terminate_as_interrupt()`` — ``contextmanager`` results are
    ``ContextDecorator``s). Inside the block, a ``SIGTERM`` raises
    ``KeyboardInterrupt`` at the interruption point, so an operation boundary's
    ``__exit__`` runs and discards the in-flight operation instead of orphaning it.

    Best-effort by design: ``signal.signal`` only works on the main thread, so an
    off-main-thread caller gets a silent no-op and run-start auto-recovery remains
    the backstop. The previous ``SIGTERM`` handler is always restored on exit, so
    nesting and post-run code are unaffected.
    """

    def _raise(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    try:
        previous = signal.signal(signal.SIGTERM, _raise)
    except (ValueError, OSError):
        # Not the main thread (or unsupported platform): prevention is unavailable
        # here; the run-start reclaim of a dead session's orphans is the backstop.
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)
