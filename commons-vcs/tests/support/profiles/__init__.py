"""Phase -1 spike profiles.

Three profiles built against the schemas in `../preflight/worked-example.md`:

    vcscore  — `vcscore/commit/v1` (genesis or projection shape)
    shepherd  — `shepherd/effect/v1`
    sgc_stub — `sgc/receipt/v1` (Phase -1 stub; not the real sgc receipt)

Validators are structural-shape checks only — no domain logic. They
reject obvious schema violations and let everything else through. The
spike's purpose is to validate that the schema and digest discipline
hold under real code, not to harden production validators.
"""

from . import sgc_stub, shepherd, vcscore

__all__ = ["sgc_stub", "shepherd", "vcscore"]
