"""APFS clonefile carrier backend for FilesystemSubstrate (macOS).

The macOS member of the ``CarrierBackend`` family (the reversibility axis,
_substrate_runtime.py): a copy-on-write specialization of the portable
``CopyCarrierBackend``. Where the overlay backends (fuse/kernel) give a union
mount, this backend makes each scope an APFS clonefile (`cp -c -R`) CoW clone of
its base — sharing blocks until write — instead of the base class's plain
recursive copy. Everything else (layer lifecycle, clone-vs-base diff,
commit/discard/push materialization) is inherited unchanged.

Lifts the proven clonefile recipe from the skeleton's LocalSandboxDevice into the
core CarrierBackend contract. Internal runtime surface — not part of the frozen
consumer SPI.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

from vcs_core._copy_carrier import CopyCarrierBackend

if TYPE_CHECKING:
    from pathlib import Path


class ClonefileCarrierBackend(CopyCarrierBackend):
    """macOS APFS clonefile carrier: each scope is a CoW clone of its base.

    Identical to :class:`CopyCarrierBackend` except each per-scope tree is built
    with APFS block-sharing (`cp -c -R`) rather than a full copy, and construction
    is gated to macOS. Off-APFS ``cp -c`` degrades to a full copy — exactly what
    the base class already does — so Linux / WSL should use the fuse or kernel
    overlay carriers, or the portable copy carrier, instead.
    """

    def _ensure_supported(self) -> None:
        if sys.platform != "darwin":
            msg = (
                "ClonefileCarrierBackend requires macOS (APFS clonefile; Linux/WSL use the "
                "fuse or kernel overlay carriers, or the portable copy carrier)."
            )
            raise RuntimeError(msg)

    def _clone_tree(self, source: Path, dest: Path) -> None:
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not source.exists():
            dest.mkdir(parents=True, exist_ok=True)
            return
        # APFS copy-on-write clone; fall back to the base class's plain recursive
        # copy off-APFS (reversibility is identical either way — only block-sharing
        # is lost).
        result = subprocess.run(
            ["cp", "-c", "-R", str(source), str(dest)], capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            super()._clone_tree(source, dest)
