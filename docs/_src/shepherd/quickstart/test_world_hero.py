"""Real-wheel execution test for the retained-run hero (world_hero.py).

The hero teaches the retained-run surface (``sp.open`` / ``tasks.register(fn)``
/ ``workspace.run(fn, ...)`` / ``output()`` / settlement), which the ``_sim``
shim does not model. So, like ``test_real_wheel_smoke.py``, this test runs the
snippet against the *installed* ``shepherd`` package, from a fresh
``shepherd init`` workspace. It is skipped when the real package is not
importable (e.g. a docs-only checkout).

The snippet runs as a real ``__main__`` script file — exactly how the docs tell
readers to run it (``python world_hero.py``), and required since 0.3.0: the
hero's bodyless ``@sp.task`` is captured at definition scope on registration,
which needs genuine on-disk source (an ``exec``-compiled string has none).

This is the executed evidence behind the homepage / Getting Started hero: the
published code block is byte-for-byte the ``hero`` snippet section this test
runs.
"""

import subprocess
import sys
from pathlib import Path

import pytest

HERO = Path(__file__).resolve().parent / "world_hero.py"


def _real_shepherd_available() -> bool:
    code = "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('shepherd') else 1)"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd="/")
    return r.returncode == 0


def _hero_snippet() -> str:
    src = HERO.read_text(encoding="utf-8")
    start = src.index("# --8<-- [start:hero]") + len("# --8<-- [start:hero]\n")
    end = src.index("# --8<-- [end:hero]")
    return src[start:end]


@pytest.mark.skipif(not _real_shepherd_available(), reason="installed shepherd package not importable")
def test_world_hero_runs_on_the_wheel(tmp_path):
    root = tmp_path / "hero-ws"
    root.mkdir()

    init_driver = tmp_path / "init_ws.py"
    init_driver.write_text(
        "from click.testing import CliRunner\n"
        "from shepherd.cli import main as shepherd_cli\n"
        f"res = CliRunner().invoke(shepherd_cli, ['init', {str(root)!r}])\n"
        "assert res.exit_code == 0, res.output\n",
        encoding="utf-8",
    )
    # cwd is the tmp dir (not this docs dir), so the _sim shim is off sys.path
    # and `import shepherd` resolves to the installed wheel.
    init = subprocess.run(
        [sys.executable, str(init_driver)], capture_output=True, text=True, cwd=str(tmp_path)
    )
    assert init.returncode == 0, init.stderr + init.stdout

    # The snippet lives OUTSIDE the workspace root (it must not pollute the
    # repo state the run forks from) and runs as a plain script with the
    # workspace as cwd — byte-for-byte the published hero block.
    snippet = tmp_path / "world_hero_snippet.py"
    snippet.write_text(_hero_snippet(), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(snippet)], capture_output=True, text=True, cwd=str(root)
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "['NOTE.txt']" in proc.stdout
    assert "Hello from a Shepherd retained output." in proc.stdout
