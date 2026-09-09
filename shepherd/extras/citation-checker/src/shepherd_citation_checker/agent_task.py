"""Registered Opus review task; its complete specification is supplied as input."""

import shepherd as sp


def review_citations(repo: sp.GitRepo, instructions: str) -> None:
    """Audit the supplied citation batch using the complete instructions argument.

    Inspect the retained source documents, discover evidence within the supplied
    budgets, and save each evidence-linked assessment promptly. This task reviews
    citations; it does not implement software or delegate to other agents.
    """
