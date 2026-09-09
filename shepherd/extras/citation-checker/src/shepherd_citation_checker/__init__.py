"""A packaged Shepherd workflow for bibliographic citation checking."""

from typing import Any

from .config import CheckerConfig

__version__ = "0.1.0"
__all__ = ["CheckerConfig", "check_paper", "check_references"]


def __getattr__(name) -> Any:
    if name in {"check_paper", "check_references"}:
        from . import api

        return getattr(api, name)
    raise AttributeError(name)
