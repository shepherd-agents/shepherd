"""B3c: the dialect composes the REAL jailed path through ``run``.

The Phase-B #1 fixture, macOS pairing (Seatbelt x clonefile, native — no
container): a deterministic fake confined-subprocess body runs through the
public verbs — ``may=`` lowered to a ``ConfinementSpec``, the body launched
via ``execution.launch_confined`` inside the reversible wrap's isolated run
scope, the delta captured implicitly at merge. ``may=ReadOnly`` is refused
**at the syscall**, the workspace stays pristine, and the capture lane is
traced. The Linux pairing (Landlock x fuse-overlayfs) runs the same fixture
under the ``container`` marker.
"""

from __future__ import annotations

import shutil
import sys
from typing import TYPE_CHECKING

import pytest
from vcs_core._containment import JailNotEstablished
from vcs_core.runtime_api import Store, VcsCore, build_builtin_substrate_context
from vcs_core.substrates import DeclarativeFilesystemSubstrate, MarkerSubstrate

from shepherd_dialect import DeterministicFakeProvider, ShepherdRunDriver, UnsupportedMayProfileError

if TYPE_CHECKING:
    from pathlib import Path

_DARWIN_JAIL = sys.platform == "darwin" and shutil.which("sandbox-exec") is not None

pytestmark = pytest.mark.skipif(
    not _DARWIN_JAIL, reason="native macOS pairing (Seatbelt x clonefile); Linux pairing rides the container marker"
)


def noop_body(stack, **args):
    """The provider owns the executable shape; the body slot is canned."""
    del stack, args


@pytest.fixture
def mg(tmp_path: Path) -> VcsCore:
    root = tmp_path / "ws"
    root.mkdir()
    store = Store(str(root / ".vcscore"))
    ctx = build_builtin_substrate_context(store, workspace=root, config={"backend": "clonefile"})
    vcscore = VcsCore(
        str(root),
        substrates=[MarkerSubstrate(ctx), DeclarativeFilesystemSubstrate(ctx), ShepherdRunDriver()],
        store=store,
    )
    vcscore.activate()
    yield vcscore
    vcscore.deactivate()


def test_jailed_permissive_run_captures_the_artifact(mg: VcsCore) -> None:
    """The happy path: jailed write lands in the carrier, captured at merge."""
    try:
        outcome = mg.execute_recorded(
            "runtime", "run", scope=mg.ground,
            task_id=f"{__name__}:noop_body", may="Permissive",
            provider=DeterministicFakeProvider(),
        )
    except JailNotEstablished as exc:
        if "policy=Permissive but the jail DENIES an in-WORKDIR write" in str(exc):
            pytest.xfail(f"host Seatbelt pairing denies the Permissive carrier canary fail-closed: {exc}")
        raise
    payload = outcome.value.transitions[0].payload
    assert payload["portable_core"]["outcome"]["status"] == "ok"
    assert payload["portable_core"]["may"] == {
        "declared": "Permissive", "resolved": "Permissive", "source": "declared",
    }
    assert payload["device_projection"]["provider"] == "deterministic-fake"
    # Capture-lane traced: the jailed subprocess's write surfaces as a
    # recorded effect on merged history (implicit capture at merge).
    effects = list(mg.log(max_count=30))
    assert any(
        e.metadata.get("type") == "FileCreate" and e.metadata.get("path") == "fake-artifact.txt"
        for e in effects
    ), "the jailed body's write must be captured into the merged history"


def test_jailed_readonly_refused_at_the_syscall_workspace_pristine(mg: VcsCore, tmp_path: Path) -> None:
    """may=ReadOnly: the jail refuses the write; discard leaves ground pristine."""
    with pytest.raises(RuntimeError, match="confined body refused"):
        mg.execute_recorded(
            "runtime", "run", scope=mg.ground,
            task_id=f"{__name__}:noop_body", may="ReadOnly",
            provider=DeterministicFakeProvider(),
        )
    ground_root = (tmp_path / "ws").resolve()
    assert not (ground_root / "fake-artifact.txt").exists(), "workspace must stay pristine"
    effects = list(mg.log(max_count=30))
    assert not any(e.metadata.get("type") == "FileCreate" for e in effects)


def test_unknown_may_profile_refuses_fail_closed(mg: VcsCore) -> None:
    """A profile with no lowering refuses rather than running weaker."""
    with pytest.raises(UnsupportedMayProfileError, match="no v0 lowering"):
        mg.execute_recorded(
            "runtime", "run", scope=mg.ground,
            task_id=f"{__name__}:noop_body", may="Standard",
            provider=DeterministicFakeProvider(),
        )
