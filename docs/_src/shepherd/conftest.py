"""Test wiring for the prototype's documented examples.

Puts the SIMULATION shim on sys.path so ``import shepherd`` resolves to
``docs_src/_sim/shepherd``. The shim models the *shape* of the surface (task
decoration, the grant spelling, workspace scoping) and, as of the 0.2.0 truth
pass, mirrors the real decoration-time validation (every task parameter must be
annotated; a docstring is optional).

Migration note (honest): the shim is NOT a drop-in for the shipped package. A
direct task call under the deterministic provider returns ``None`` in 0.2.0 —
the real value/output flows through the retained-run surface
(``workspace.run(...).output()``), which the shim does not model. Rewriting the
example teaching from direct-call-returns-value to the real retained-run shape
is tracked as the docs-site adoption tranche (ISS-006), not this conftest.
``test_real_wheel_smoke.py`` guards the *import* surface against the shim
drifting from the real package (e.g. inventing modules the wheel lacks).
"""

import sys
from pathlib import Path

_DOCS_SRC = Path(__file__).resolve().parent
_SIM = _DOCS_SRC / "_sim"
for _p in (str(_SIM), str(_DOCS_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
