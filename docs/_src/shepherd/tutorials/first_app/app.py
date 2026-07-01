"""First Shepherd app — the tutorial's running example (tested in CI).

Included into the tutorial page via pymdownx.snippets sections; do not rename
markers. Task docstrings here are BEHAVIOR (the model-call goal) — editing
them requires re-recording transcripts (DESIGN ground rule 3).
"""

# --8<-- [start:setup]
from dataclasses import dataclass

import shepherd as shp
from shepherd.providers import claude


@dataclass(frozen=True)
class Triage:
    category: str   # bugfix | feature | docs | refactor
    priority: str   # low | medium | high
    rationale: str
# --8<-- [end:setup]


# --8<-- [start:triage]
@shp.task
def triage_change(diff: str) -> Triage:
    """Classify this code change.

    Categories are bugfix, feature, docs, and refactor.
    Priority reflects user impact, not engineering effort.
    """
# --8<-- [end:triage]


# --8<-- [start:review]
@dataclass(frozen=True)
class Review:
    summary: str
    verdict: str    # approve | request-changes


@shp.task
def write_review(diff: str, triage: Triage) -> Review:
    """Write a short review for this change, given its triage."""


def review_change(diff: str) -> Review:
    return write_review(diff, triage_change(diff))
# --8<-- [end:review]


SAMPLE_DIFF = """\
diff --git a/auth.py b/auth.py
@@ -42,7 +42,7 @@
-    if user.is_admin:
+    if user.is_admin or user.has_role("admin"):
"""


def main() -> Review:
    # --8<-- [start:run]
    with shp.workspace(model=claude("sonnet-4-5")):
        triage = triage_change(SAMPLE_DIFF)
        review = review_change(SAMPLE_DIFF)
    # --8<-- [end:run]
    print(f"{triage.category}/{triage.priority}: {review.verdict} - {review.summary}")
    return review


if __name__ == "__main__":
    main()
