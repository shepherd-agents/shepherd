"""The dialect's policy lowering: ``may=`` → ``ConfinementSpec`` (B3c-1).

``may=`` is the single policy surface; ``ConfinementSpec`` is its lowered IR
(`decisions.md` ``confinement-spec-lowered-ir``). The dialect owns the
vocabulary and the lowering; vcs-core's jail enforces the spec and stays
``may=``-blind. v0 lowers the two live profile names exactly (byte-parity
with the live profiles by construction — the spec lowers onto the same
profile names the backends compile); anything else refuses fail-closed
(``Standard``/``ModelOnly`` are signposted, not built — runtime-call-api.md §7).

The default is loud (`decisions.md` ``may-default-is-permissive``, amended
2026-06-10): ``resolve_may`` is the single resolution point for the
``may=None → Permissive`` rule, and it returns provenance
(``declared``/``resolved``/``source``) the run payload records — so the
defaulted population is countable, the same discipline
``reversible-by-default`` applies to the non-reversible opt-out.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vcs_core.spi import ConfinementSpec

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = [
    "BindingRootGrant",
    "MayResolution",
    "OverlappingBoundRootsError",
    "UnsupportedMayProfileError",
    "lower_grants_to_confinement",
    "lower_may_resolution_to_confinement",
    "lower_may_to_confinement",
    "resolve_may",
    "validate_disjoint_roots",
]


class UnsupportedMayProfileError(ValueError):
    """The declared ``may=`` profile has no v0 lowering — refuse, never weaken."""


@dataclass(frozen=True)
class MayResolution:
    """The lowering's provenance: what the author wrote vs what the run got.

    ``declared`` is exactly the author's declaration (``None`` = omitted);
    ``resolved`` is the profile the run actually lowers to; ``source`` is
    ``"declared"`` or ``"defaulted"``. Recorded into the run payload so a
    defaulted-Permissive run never masquerades as a declared one.
    """

    declared: str | None
    resolved: str
    source: str

    def as_record(self) -> dict[str, str | None]:
        return {"declared": self.declared, "resolved": self.resolved, "source": self.source}


def resolve_may(may: str | None) -> MayResolution:
    """Resolve a (possibly omitted) ``may=`` declaration — the one default site."""
    if may is None:
        return MayResolution(declared=None, resolved="Permissive", source="defaulted")
    return MayResolution(declared=may, resolved=may, source="declared")


def lower_may_to_confinement(may: str | None, working_path: Path) -> ConfinementSpec:
    """Lower a declared profile name onto the generic confinement IR."""
    return lower_may_resolution_to_confinement(resolve_may(may), working_path)


def lower_may_resolution_to_confinement(
    resolution: MayResolution,
    working_path: Path,
) -> ConfinementSpec:
    """Lower an already-resolved ``may=`` declaration onto the confinement IR."""
    if resolution.resolved == "Permissive":
        return ConfinementSpec.permissive_for(working_path)
    if resolution.resolved == "ReadOnly":
        return ConfinementSpec.read_only()
    raise UnsupportedMayProfileError(
        f"may={resolution.declared!r} has no v0 lowering (live profiles: 'ReadOnly', 'Permissive'; "
        "'Standard'/'ModelOnly' are signposted, not built)."
    )


# --- v0.2: per-binding grant lowering -------------------------------------------------
#
# Where ``may=`` lowers one whole-workspace profile, per-binding grants lower a *set* of
# writable roots — one bound ``GitRepo``/``Folder`` per parameter, each granted ReadOnly or
# ReadWrite in the task signature. The lowering is deliberately whole-root per binding
# (∅-or-all), which is what keeps it sound without the two walls (W4 ``commit_prepared`` and
# the SPI binding-discriminator): a within-binding proper subset is Tier-3, excluded here.


class OverlappingBoundRootsError(ValueError):
    """Bound roots overlap or nest — refuse, because nesting is sub-root semantics (Tier-3).

    The syscall jail's writable-root rules are *additive* (a write is allowed beneath ANY
    writable root), so a ReadOnly root nested inside a ReadWrite root would be silently
    writable. Whole-root-per-binding soundness therefore requires disjoint roots; overlap
    fails closed here (at bind time), never at the jail.
    """


@dataclass(frozen=True)
class BindingRootGrant:
    """One bound root and whether its per-binding grant makes the subtree writable.

    The pure input to per-binding jail lowering, decoupled from the ``May[GitRepo, ...]``
    annotation machinery (Lane C wires captured grants to this). ``writable`` is True for a
    ReadWrite grant, False for ReadOnly.
    """

    binding: str
    root: str
    writable: bool


def _fold_path(path: Path) -> Path:
    """Case- and Unicode-fold a path for filesystem-aliasing-aware comparison.

    ``os.path.realpath`` resolves symlinks and ``..`` but does **not** case-fold or
    Unicode-normalize on macOS APFS (case-insensitive) — so ``realpath("backend")`` and
    ``realpath("BACKEND")`` are distinct strings that name the *same* directory. Folding
    (NFC-normalize + casefold each part) makes such aliases compare equal, so the nesting test
    below catches case-/Unicode-aliased pairs as well. Fail-closed direction: on a genuinely
    case-sensitive filesystem this over-rejects two roots that differ only by case/normalization
    (a pathological, easily-renamed bind config) — the safe trade for a security guard.
    """
    return Path(*(unicodedata.normalize("NFC", part).casefold() for part in path.parts))


def _roots_overlap(a: Path, b: Path) -> bool:
    """Whether two realpath'd bound roots name the same subtree or one nests in the other.

    Three fail-closed lenses: (1) exact realpath string equality/nesting (symlinks/``..`` already
    resolved); (2) inode identity via :func:`os.path.samefile` for roots that exist — the
    filesystem's own truth, catching case/Unicode/hardlink aliases to the *same* directory even
    when the realpath strings differ; (3) case-/Unicode-folded equality/nesting, catching aliased
    *nesting* (and aliases whose targets do not yet exist, where ``samefile`` cannot speak).
    """
    if a == b or a.is_relative_to(b) or b.is_relative_to(a):
        return True
    try:
        if a.samefile(b):  # same inode ⇒ same dir regardless of case/normalization
            return True
    except OSError:
        pass  # one side does not exist yet — the fold lens still covers aliasing
    fa, fb = _fold_path(a), _fold_path(b)
    return fa == fb or fa.is_relative_to(fb) or fb.is_relative_to(fa)


def validate_disjoint_roots(roots: Iterable[str]) -> tuple[str, ...]:
    """Fail closed unless every bound root is disjoint (none aliases or nests inside another).

    Returns the canonicalized (realpath) roots on success. Raises
    :class:`OverlappingBoundRootsError` if any two roots name the same subtree or one contains the
    other — the excluded sub-root case (§4 precondition): allow nesting and you have silently
    re-entered Tier-3. Overlap is judged by :func:`_roots_overlap`, which treats
    filesystem-aliased roots (case-insensitive APFS, Unicode NFC/NFD, hardlinks) as overlapping so
    an alias cannot smuggle a ReadOnly subtree into a ReadWrite root's writable set.
    """
    canonical = [Path(os.path.realpath(str(root))) for root in roots]
    for i, a in enumerate(canonical):
        for b in canonical[i + 1 :]:
            if _roots_overlap(a, b):
                raise OverlappingBoundRootsError(
                    f"bound roots overlap, alias, or nest: {a} vs {b}. Per-binding grants require "
                    "disjoint roots (a nested or filesystem-aliased root is sub-root semantics — "
                    "Tier-3, excluded from v0.2)."
                )
    return tuple(str(root) for root in canonical)


def lower_grants_to_confinement(grants: Sequence[BindingRootGrant]) -> ConfinementSpec:
    """Lower per-binding grants to a deny-closed, multi-root ``ConfinementSpec``.

    ``writable_roots`` is the union of the roots whose grant is ReadWrite; ReadOnly-granted roots
    contribute no writable root (their subtree is denied at the syscall). The spec's network
    axis defaults to deny-all — v0.2 makes no network claim. Bound roots must be disjoint
    (:func:`validate_disjoint_roots`), so the union is itself a set of whole, non-overlapping
    roots — each ∅-or-all — which is why this lowering needs neither the W4 commit-exact
    primitive nor the SPI binding-discriminator.
    """
    validate_disjoint_roots(grant.root for grant in grants)
    writable_roots = tuple(os.path.realpath(grant.root) for grant in grants if grant.writable)
    return ConfinementSpec(writable_roots=writable_roots)
