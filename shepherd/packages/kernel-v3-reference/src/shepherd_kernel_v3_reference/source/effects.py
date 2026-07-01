"""Effect signatures and registry (§02, §10).

§10 separates three schemas:

- `payload_schema_ref`           : checked at `Perform`
- `operation_result_schema_ref`  : checked at `resume(value)`
- `handled_result_schema_ref`    : checked at `Answer`/`Abort`

The first two live on the `EffectSignature`; the third is threaded through
`HandlerInstall`, since it depends on the handled computation rather than on
the operation alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shepherd_kernel_v3_reference.schemas import Schema


@dataclass(frozen=True)
class EffectSignature:
    effect_kind: str
    payload_schema: Schema
    operation_result_schema: Schema


class EffectRegistry:
    """A mapping from effect_kind to EffectSignature."""

    def __init__(self) -> None:
        self._sigs: dict[str, EffectSignature] = {}

    def register(self, sig: EffectSignature) -> None:
        if sig.effect_kind in self._sigs:
            raise ValueError(f"effect already registered: {sig.effect_kind}")
        self._sigs[sig.effect_kind] = sig

    def lookup(self, effect_kind: str) -> EffectSignature:
        try:
            return self._sigs[effect_kind]
        except KeyError as exc:
            raise KeyError(f"unknown effect: {effect_kind}") from exc

    def __contains__(self, effect_kind: str) -> bool:
        return effect_kind in self._sigs
