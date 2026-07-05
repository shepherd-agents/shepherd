"""Real-wheel smoke: guard the docs' import surface against the simulation shim
drifting from the shipped `shepherd` package.

The rest of this directory imports the ``_sim`` shim (see conftest). This test
deliberately does NOT: it runs a clean subprocess with the shim OFF the path and
imports the *installed* ``shepherd`` package, asserting the symbols the docs
teach actually exist in the wheel (and that the fenced surface does not). It is
skipped if the real package is not importable (e.g. a docs-only checkout).

This is the guard that would have caught the pre-0.2.0 docs teaching
``from shepherd.providers import claude`` — a module the wheel does not have.
"""

import subprocess
import sys
import textwrap

import pytest

# Names the docs teach as the public 0.2.0 surface.
REQUIRED = [
    "task", "workspace", "May", "GitRepo", "ReadOnly", "ReadWrite",
    "ShepherdWorkspace", "WorkspaceRun", "RunOutput", "Changeset",
]
# The path-scoped grant spelling is fenced out of the public facade in 0.2.0.
FORBIDDEN = ["GitRepoPath"]


def _real_shepherd_available() -> bool:
    # Import in a clean subprocess with THIS docs dir (and its _sim) off sys.path,
    # so we resolve the installed package, not the shim.
    code = "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('shepherd') else 1)"
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, cwd="/",
    )
    return r.returncode == 0


@pytest.mark.skipif(not _real_shepherd_available(), reason="installed shepherd package not importable")
def test_docs_symbols_exist_in_the_real_wheel():
    script = textwrap.dedent(
        f"""
        import shepherd as sp
        missing = [n for n in {REQUIRED!r} if not hasattr(sp, n)]
        present_forbidden = [n for n in {FORBIDDEN!r} if hasattr(sp, n)]
        assert not missing, f"docs teach symbols absent from the wheel: {{missing}}"
        assert not present_forbidden, f"docs must not expose fenced symbols: {{present_forbidden}}"
        assert sp.__version__, "no __version__"
        # The provider module the pre-0.2.0 docs invented must not exist.
        import importlib.util
        assert importlib.util.find_spec("shepherd.providers") is None, "shepherd.providers should not exist"
        print("ok", sp.__version__)
        """
    )
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, cwd="/")
    assert r.returncode == 0, f"real-wheel smoke failed:\n{r.stdout}\n{r.stderr}"
