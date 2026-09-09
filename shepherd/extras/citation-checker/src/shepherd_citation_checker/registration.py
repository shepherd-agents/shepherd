"""Shepherd package-discovery metadata."""

from typing import Any

from shepherd_core.package import package

from . import __version__


@package(name="citation_checker", version=__version__, tasks=["shepherd_citation_checker.tasks"])
def citation_checker() -> Any:
    """Check bibliography identity and metadata, with evidence and corrections."""
