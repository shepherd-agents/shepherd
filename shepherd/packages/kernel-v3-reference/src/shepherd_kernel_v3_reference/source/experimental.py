"""Explicit opt-in surface for publication-control spike syntax.

These forms are useful for exploring the publication profile, but they are not
part of the validated Core-A source surface and should not be imported from the
package root.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from shepherd_kernel_v3_reference.profiles import PUBLICATION_EXPERIMENTAL
from shepherd_kernel_v3_reference.source.syntax import Abort, Expr, Handle, Let, Perform, Resume, Return


@dataclass(frozen=True)
class Forward:
    """Handler-side explicit decline of the selected binding instance."""


@dataclass(frozen=True)
class TerminalDelay:
    """Terminal delayed use of the selected worker continuation."""

    reason: Expr


@dataclass(frozen=True)
class TerminalFork:
    """Terminal branch-scoped uses of the selected worker continuation."""

    branches: tuple[tuple[str, Expr], ...]


PublicationExperimentalComputation = Union[
    Return,
    Let,
    Perform,
    Handle,
    Resume,
    Abort,
    Forward,
    TerminalDelay,
    TerminalFork,
]

__all__ = [
    "PUBLICATION_EXPERIMENTAL",
    "Forward",
    "PublicationExperimentalComputation",
    "TerminalDelay",
    "TerminalFork",
]
