"""Durable-vocabulary hygiene (W1a).

Durable tokens — settlement-policy kind strings, the canonical trace domain —
are SERIALIZED. A raw literal that diverges from the constant it should be
(the class of bug behind the C2 fenced-run-start needle overload,
`_p030_enabled` → `_enabled`) is silent and dangerous. These guards keep each
durable token in exactly one constant home, and pin the cross-package token
that dialect borrows from shepherd2.
"""

from __future__ import annotations

import re
from pathlib import Path

from shepherd2.kernel.canonical import CANONICAL_VERSION

from shepherd_dialect.trace import SHEPHERD_KERNEL_DOMAIN, VCSCORE_DOMAIN
from shepherd_dialect.workspace_control.schemas import (
    FILESYSTEM_AUTHORITY_TERMINALIZATION_KIND,
    RETAINED_OUTPUT_SELECTION_KIND,
)

_DIALECT_SRC = Path(__file__).resolve().parents[1] / "src" / "shepherd_dialect"

# token -> the single module basename allowed to contain its raw literal (its
# constant home). Any other raw occurrence in dialect src is a divergence risk.
_DURABLE_TOKEN_HOMES = {
    RETAINED_OUTPUT_SELECTION_KIND: "schemas.py",
    FILESYSTEM_AUTHORITY_TERMINALIZATION_KIND: "schemas.py",
    VCSCORE_DOMAIN: "trace.py",
}


def test_durable_tokens_have_a_single_constant_home() -> None:
    """No durable token appears as a raw literal outside its constant home."""
    offenders: dict[str, list[str]] = {}
    for path in sorted(_DIALECT_SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for token, home in _DURABLE_TOKEN_HOMES.items():
            if path.name == home:
                continue
            # match the token only as a quoted string literal
            if re.search(rf'"{re.escape(token)}"', text):
                offenders.setdefault(path.name, []).append(token)
    assert offenders == {}, (
        f"durable token used as a raw literal outside its constant home — import the constant instead: {offenders}"
    )


def test_shepherd_kernel_domain_agrees_with_shepherd2_canonical_version() -> None:
    """The cross-package token dialect borrows from shepherd2 stays in agreement."""
    assert SHEPHERD_KERNEL_DOMAIN == CANONICAL_VERSION == "shepherd.kernel.canonical.v2"


def test_vcscore_domain_is_the_expected_canonical_string() -> None:
    """The dialect-owned vcs-core trace domain constant is pinned."""
    assert VCSCORE_DOMAIN == "vcscore.canonical.v2"
