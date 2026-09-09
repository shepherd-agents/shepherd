"""Retained, provider-owned citation checking task."""

import shepherd as sp


def check_references(repo: sp.GitRepo, instructions: str, input_path: str, output_path: str) -> None:
    """Audit the supplied bibliography entries against external evidence.

    Read input_path, policy.md and instructions. The JSON input includes raw
    references and prefetched public-source evidence, with full responses saved
    under evidence/. Those records are candidates, not verified matches.
    Check EVERY supplied reference independently. Use WebSearch/WebFetch or
    Python HTTP requests for missing or ambiguous evidence, including alternate
    versions, author lists and venue existence. Save additional fetched evidence
    or search results under evidence/extra/ with source URL, retrieval time and
    the actual relevant text. Do not fabricate retrievals or URLs. Use no API
    keys and do not access credentials. Do not create subagents or delegate.

    Write output_path as one JSON object with `results`, one object per input id.
    Each result must include: id, status (valid, valid_artifact,
    correction_needed, hallucinated, needs_sac_review, incomplete), reason,
    matched_title (string or null), title_finding, authors_finding, venue_finding,
    evidence (list of {url, evidence_file, supports}), searches (list of
    {query, outcome}), and meta_review_note (string). evidence_file must name a
    real file in this workspace holding retrieved evidence for that URL.
    For negative existence findings explain completed search coverage. For
    authorship-based hallucination specify the cited and actual authors.
    Use `incomplete` rather than guessing when retrieval/search fails.

    Write the complete JSON promptly, within the four-minute execution budget.
    Do not install packages, modify inputs or policy, or write anything except
    output_path and evidence/extra/. The task is to audit, not build software.
    """
