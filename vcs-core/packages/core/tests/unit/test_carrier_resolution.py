"""Carrier auto-resolution.

``backend=None`` must resolve to a working carrier on every platform: a native
overlay when present, the macOS APFS clonefile carrier on darwin, and the
portable copy carrier as the universal floor otherwise — so an isolated run
never lacks a carrier.

``_auto_detect_backend_name`` reads only module-level probes
(``detect_overlay_backend``/``_platform_name``), not instance state, so a bare
instance is sufficient — no substrate bootstrap required.
"""

from __future__ import annotations

import pytest
from vcs_core import substrates


def _substrate():
    return object.__new__(substrates.FilesystemSubstrate)


@pytest.mark.parametrize(
    ("native", "platform", "expected"),
    [
        ("kernel", "linux", "kernel"),  # native overlay preferred when available
        ("fuse", "linux", "fuse"),
        (None, "darwin", "clonefile"),  # macOS APFS default
        (None, "linux", "copy"),  # bare Linux / WSL floors to the portable carrier
        (None, "win32", "copy"),  # anything without a native overlay floors to copy
    ],
)
def test_auto_backend_resolution_never_returns_none(monkeypatch, native, platform, expected) -> None:
    monkeypatch.setattr(substrates, "detect_overlay_backend", lambda: native)
    monkeypatch.setattr(substrates, "_platform_name", lambda: platform)
    assert _substrate()._auto_detect_backend_name() == expected
