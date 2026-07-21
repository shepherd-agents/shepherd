"""S3: the hermes lane under the real jail, Linux pairing (Landlock x fuse-overlayfs).

The executed evidence that moves the hermes provider's ``confined=True`` from
plausible to recorded (execplan 260709 r5 §S3), in the
``test_jailed_run_linux.py`` fixture shape. Unlike the fake-provider file this
lane is auth-needing and network-reaching, so it additionally gates on the
``hermes`` CLI and an ``ANTHROPIC_API_KEY`` — release evidence to run on
demand, never a CI gate.

Asserts the §4.6 tree-reap: the Linux hard stop is the subreaper supervisor
(``_reaper.py``), so a command child spawned into its own session — even a
daemon that reparents to init before the alarm — is killed with the tree, not
left surviving. (This was a strict-xfail canary until the reaper landed; the
S3 evidence run is what exposed the reparent-escape the naive walk missed.)
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from typing import TYPE_CHECKING

import pytest
from vcs_core.runtime_api import Store, VcsCore, build_builtin_substrate_context
from vcs_core.substrates import DeclarativeFilesystemSubstrate, MarkerSubstrate

from shepherd_dialect import HermesHeadlessProvider, ShepherdRunDriver
from shepherd_dialect.nucleus import BudgetExhausted

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [
    pytest.mark.container,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux pairing (Landlock x fuse-overlayfs)"),
    pytest.mark.skipif(shutil.which("hermes") is None, reason="needs the hermes CLI on PATH"),
    pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY (live evidence run)"),
]

_MODEL = os.environ.get("SHEPHERD_HERMES_MODEL", "claude-haiku-4-5")


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


def test_jailed_hermes_run_captures_the_artifact(mg: VcsCore, tmp_path: Path) -> None:
    """The confined=True evidence: a jailed hermes write captured at merge,
    with the scratch scrubbed out of the delta and tool events on the trace."""
    outcome = mg.execute_recorded(
        "runtime",
        "run",
        scope=mg.ground,
        task_id=f"{__name__}:noop_body",
        may="Permissive",
        provider=HermesHeadlessProvider(
            prompt="Create hermes-artifact.txt in the current directory containing exactly: jailed hermes ok",
            model=_MODEL,
            model_provider="anthropic",
            budget_seconds=180,
        ),
    )
    payload = outcome.value.transitions[0].payload
    assert payload["portable_core"]["outcome"]["terminal"] == "success"
    effects = list(mg.log(max_count=40))
    assert any(
        e.metadata.get("type") == "FileCreate" and e.metadata.get("path") == "hermes-artifact.txt" for e in effects
    )
    # The D3 scrub held under the jail: no housekeeping entered the delta.
    assert not any(".hermes-scratch" in str(e.metadata.get("path", "")) for e in effects)


def test_jailed_hermes_alarm_kill_is_exhausted_with_evidence(mg: VcsCore) -> None:
    """The budget stop under the real jail: rc -14 maps to BudgetExhausted and
    the exception carries the started bookend (r5: evidence rides budget stops)."""
    with pytest.raises(BudgetExhausted) as excinfo:
        mg.execute_recorded(
            "runtime",
            "run",
            scope=mg.ground,
            task_id=f"{__name__}:noop_body",
            may="Permissive",
            provider=HermesHeadlessProvider(
                prompt="Write a very long essay, one file per paragraph, at least twenty files.",
                model=_MODEL,
                model_provider="anthropic",
                budget_seconds=15,
            ),
        )
    kinds = [event.kind for event in excinfo.value.provider_events]
    assert kinds, "the exhausted run must carry evidence on the exception"
    assert kinds[0] == "provider.invocation.started"


def test_jailed_hermes_alarm_kill_reaps_the_process_tree(mg: VcsCore, tmp_path: Path) -> None:
    """§4.6 landed: the alarm kills the whole tree, so no command child survives
    (was a strict-xfail canary until the subreaper reaper landed)."""
    import subprocess
    import uuid

    token = f"hermes-orphan-{uuid.uuid4().hex[:12]}"
    orphan_marker = f"{token}.txt"
    try:
        with pytest.raises(BudgetExhausted) as excinfo:
            mg.execute_recorded(
                "runtime",
                "run",
                scope=mg.ground,
                task_id=f"{__name__}:noop_body",
                may="Permissive",
                provider=HermesHeadlessProvider(
                    prompt=(
                        "First, run this exact shell command with the terminal tool, immediately: "
                        f"nohup bash -c 'sleep 300 && echo escaped > {orphan_marker}' >/dev/null 2>&1 & "
                        "Then run: sleep 300"
                    ),
                    model=_MODEL,
                    model_provider="anthropic",
                    budget_seconds=75,
                ),
            )
        # Non-vacuous gate: the canary only means something if the terminal
        # call actually happened before the alarm (the harvested transcript
        # rides the exception — r5). Otherwise the run is inconclusive.
        spawned = any(
            event.kind == "tool.call.started" and event.payload.get("canonical_tool_name") == "bash"
            for event in excinfo.value.provider_events
        )
        if not spawned:
            pytest.skip("inconclusive: the budget expired before the terminal call — no child was ever spawned")
        time.sleep(2)  # let the process table settle after the SIGALRM kill
        survivors = subprocess.run(["pgrep", "-f", token], capture_output=True, text=True, check=False)
        # The reaper killed the tree: no live survivor, and (belt-and-braces) the
        # ground never gained the orphan's write.
        assert survivors.returncode != 0, (
            f"a command child survived the alarm kill as a live process: pids {survivors.stdout.split()}"
        )
        assert not (tmp_path / "ws" / orphan_marker).exists(), "the orphan's write reached the ground workspace"
    finally:
        subprocess.run(["pkill", "-f", token], capture_output=True, check=False)
