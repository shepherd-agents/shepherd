"""Shepherd Authoring - Design-refinement pipeline for the Shepherd framework."""

from __future__ import annotations

from shepherd_core.package import package

__version__ = "0.3.0"

from shepherd_authoring.checks import (
    check_document_structure,
    check_file_exists,
    check_refinement_log,
    check_version_history,
)
from shepherd_authoring.models import CritiqueOutput
from shepherd_authoring.tasks import (
    CritiqueDocuments,
    DraftSpikePlan,
    ExtractPrinciples,
    RefineDocuments,
)
from shepherd_authoring.workflows.design_refinement import (
    CritiqueRefineLoop,
    DesignRefinement,
    PlanDesignRefinement,
    RunDesignRefinement,
)
from shepherd_authoring.workflows.design_review import DesignReview


@package(
    name="authoring",
    version=__version__,
    tasks=["shepherd_authoring.tasks"],
)
def authoring() -> None:
    """Automated design document refinement pipeline."""


__all__ = [
    # Leaf tasks
    "CritiqueDocuments",
    # Models
    "CritiqueOutput",
    # Workflow orchestrators
    "CritiqueRefineLoop",
    "DesignRefinement",
    "DesignReview",
    "DraftSpikePlan",
    "ExtractPrinciples",
    "PlanDesignRefinement",
    "RefineDocuments",
    "RunDesignRefinement",
    # Version
    "__version__",
    # Package
    "authoring",
    # Check predicates
    "check_document_structure",
    "check_file_exists",
    "check_refinement_log",
    "check_version_history",
]
