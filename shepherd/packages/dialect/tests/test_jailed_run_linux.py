"""B3c-4: the same jailed fixture, Linux pairing (Landlock x fuse-overlayfs).

Runs under the ``container`` marker inside the privileged Podman container
(``make test_container`` from ``vcs-core/packages/core``) — the second half
of the B3c both-pairings acceptance. Same fixture shape as the macOS file:
``may=`` lowered, body launched via ``launch_confined`` in the reversible
wrap's isolated scope, delta captured at merge; ``ReadOnly`` refused at the
syscall with the workspace pristine.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest
from vcs_core.runtime_api import Store, VcsCore, build_builtin_substrate_context
from vcs_core.substrates import DeclarativeFilesystemSubstrate, MarkerSubstrate

from shepherd_dialect import DeterministicFakeProvider, ShepherdRunDriver

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [
    pytest.mark.container,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux pairing (Landlock x fuse-overlayfs)"),
]


def noop_body(stack, **args):
    """The provider owns the executable shape; the body slot is canned."""
    del stack, args


@pytest.fixture
def mg(tmp_path: Path) -> VcsCore:
    root = tmp_path / "ws"
    root.mkdir()
    store = Store(str(root / ".vcscore"))
    ctx = build_builtin_substrate_context(store, workspace=root, config={"backend": "fuse"})
    vcscore = VcsCore(
        str(root),
        substrates=[MarkerSubstrate(ctx), DeclarativeFilesystemSubstrate(ctx), ShepherdRunDriver()],
        store=store,
    )
    vcscore.activate()
    yield vcscore
    vcscore.deactivate()


def test_jailed_permissive_run_captures_the_artifact(mg: VcsCore) -> None:
    """Happy path on the Linux pairing: jailed write captured at merge."""
    outcome = mg.execute_recorded(
        "runtime", "run", scope=mg.ground,
        task_id=f"{__name__}:noop_body", may="Permissive",
        provider=DeterministicFakeProvider(),
    )
    payload = outcome.value.transitions[0].payload
    assert payload["portable_core"]["outcome"]["status"] == "ok"
    effects = list(mg.log(max_count=30))
    assert any(
        e.metadata.get("type") == "FileCreate" and e.metadata.get("path") == "fake-artifact.txt"
        for e in effects
    )


def test_jailed_readonly_refused_at_the_syscall_workspace_pristine(mg: VcsCore, tmp_path: Path) -> None:
    """may=ReadOnly: Landlock refuses the write; ground stays pristine."""
    with pytest.raises(RuntimeError, match="confined body refused"):
        mg.execute_recorded(
            "runtime", "run", scope=mg.ground,
            task_id=f"{__name__}:noop_body", may="ReadOnly",
            provider=DeterministicFakeProvider(),
        )
    ground_root = (tmp_path / "ws").resolve()
    assert not (ground_root / "fake-artifact.txt").exists()
    effects = list(mg.log(max_count=30))
    assert not any(e.metadata.get("type") == "FileCreate" for e in effects)
