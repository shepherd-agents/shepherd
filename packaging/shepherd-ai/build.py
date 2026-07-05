#!/usr/bin/env python3
"""Assemble the single bundled ``shepherd-ai`` distribution for PyPI.

The repository is a uv workspace of many small packages (``shepherd``,
``shepherd-core``, ``vcs-core``, ``commons-vcs`` ...). Several of those
distribution names are already taken on PyPI by unrelated projects, so the
public release is shipped as ONE self-contained wheel named ``shepherd-ai``
that vendors the whole runtime import closure.

This script is non-destructive: it copies the relevant ``src/<pkg>`` trees into
a staging directory, generates a consolidated ``pyproject.toml`` (metadata +
plugin entry points + extras), reconciles the in-code ``shepherd.__version__``
with the release version, and builds the sdist + wheel with ``uv build``. The
workspace itself is never modified.

Usage:
    python packaging/shepherd-ai/build.py           # build sdist + wheel into dist/
    python packaging/shepherd-ai/build.py --version 0.2.0
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
STAGE = HERE / "build" / "stage"
DEFAULT_VERSION = "0.2.0"

# Bundled import package  ->  its src/ directory in the workspace.
# This is the runtime install closure of `shepherd[providers,contexts]`:
# the deterministic quickstart plus the Claude/OpenAI provider lanes.
PACKAGES = {
    "shepherd": "shepherd/packages/meta/src/shepherd",
    "shepherd_core": "shepherd/packages/core/src/shepherd_core",
    "shepherd_runtime": "shepherd/packages/runtime/src/shepherd_runtime",
    "shepherd_export": "shepherd/packages/export/src/shepherd_export",
    "shepherd_dialect": "shepherd/packages/dialect/src/shepherd_dialect",
    "shepherd_kernel_v3_reference": "shepherd/packages/kernel-v3-reference/src/shepherd_kernel_v3_reference",
    "shepherd2": "shepherd2/src/shepherd2",
    "vcs_core": "vcs-core/packages/core/src/vcs_core",
    "commons_vcs": "commons-vcs/src/commons_vcs",
    "shepherd_providers": "shepherd/packages/providers/src/shepherd_providers",
    "shepherd_contexts": "shepherd/packages/contexts/src/shepherd_contexts",
}

# Plugin entry points discovered at runtime via importlib.metadata. When the
# sub-packages collapse into one wheel their entry points must be re-declared
# here, or discovery silently finds nothing. Keep in sync with the source
# packages' pyproject.toml [project.entry-points.*] tables.
ENTRY_POINTS = """\
[project.entry-points."shepherd.providers"]
claude = "shepherd_providers.claude:ClaudeProvider"
openai = "shepherd_providers.openai:OpenAIProvider"

[project.entry-points."shepherd.contexts"]
workspace = "shepherd_contexts.workspace:WorkspaceRef"
simple_workspace = "shepherd_contexts.simple_workspace:SimpleWorkspace"
session = "shepherd_contexts.session:SessionState"
mcp = "shepherd_contexts.mcp:MCPServerContext"
database = "shepherd_contexts.database:DatabaseContext"
kvstore = "shepherd_contexts.kvstore:KVStoreContext"
appstore = "shepherd_contexts.appstore:AppStoreContext"

[project.entry-points."shepherd.effects"]
workspace = "shepherd_contexts.workspace.effects"
session = "shepherd_contexts.session.effects"
simple_workspace = "shepherd_contexts.simple_workspace.effects"
kvstore = "shepherd_contexts.kvstore.effects"
mcp = "shepherd_contexts.mcp.effects"
appstore = "shepherd_contexts.appstore.effects"
database = "shepherd_contexts.database.effects"

[project.entry-points."vcscore.substrate_plugins"]
"shepherd.run_driver" = "shepherd_dialect.plugin:RUN_DRIVER_PLUGIN"
"shepherd.task_ledger" = "shepherd_dialect.plugin:TASK_LEDGER_PLUGIN"
"shepherd.task_artifacts" = "shepherd_dialect.plugin:TASK_ARTIFACT_PLUGIN"
"shepherd.run_ledger" = "shepherd_dialect.plugin:RUN_LEDGER_PLUGIN"
"""

PYPROJECT = """\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "shepherd-ai"
version = "%(version)s"
description = "Shepherd: Programmable meta-agents via reversible agentic execution traces"
readme = "README.md"
requires-python = ">=3.11"
license = "MIT"
license-files = ["LICENSE"]
keywords = ["agent", "llm", "claude", "openai", "ai", "effects", "multi-provider", "meta-agent"]
authors = [
    { name = "Simon Yu" },
    { name = "Derek Chong" },
]
classifiers = [
    "Development Status :: 3 - Alpha",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
    "Programming Language :: Python :: 3.14",
    "Topic :: Scientific/Engineering :: Artificial Intelligence",
    "Typing :: Typed",
]
dependencies = [
    "click>=8.0",
    "pydantic>=2.0",
    "pygit2>=1.13.0",
    "tomli-w>=1.0",
    "tomli>=2.0; python_version < '3.11'",
]

[project.optional-dependencies]
claude = ["claude-agent-sdk>=0.1.0"]
openai = ["openai>=1.66"]
all = ["shepherd-ai[claude,openai]"]

[project.scripts]
shepherd = "shepherd.cli:main"
sp = "shepherd.cli:main"

[project.urls]
Homepage = "https://shepherd-agents.ai/"
Documentation = "https://docs.shepherd-agents.ai/"
Repository = "https://github.com/shepherd-agents/shepherd"
Issues = "https://github.com/shepherd-agents/shepherd/issues"

%(entry_points)s
[tool.hatch.build.targets.wheel]
packages = [
%(wheel_packages)s
]

[tool.hatch.build.targets.sdist]
include = ["/src", "/README.md", "/LICENSE"]
"""


def stage(version: str) -> None:
    """Assemble the bundled source tree and pyproject under STAGE."""
    if STAGE.exists():
        shutil.rmtree(STAGE)
    src = STAGE / "src"
    src.mkdir(parents=True)

    missing = [rel for rel in PACKAGES.values() if not (REPO / rel).is_dir()]
    if missing:
        raise SystemExit("MISSING source dirs:\n  " + "\n  ".join(missing))

    for imp, rel in PACKAGES.items():
        shutil.copytree(
            REPO / rel,
            src / imp,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

    # Reconcile the in-code framework version with the release version so
    # `import shepherd; shepherd.__version__` agrees with the wheel's version.
    init = src / "shepherd" / "__init__.py"
    text = init.read_text()
    new_text, n = re.subn(
        r'^__version__\s*=\s*["\'].*["\']',
        f'__version__ = "{version}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if n != 1:
        raise SystemExit("could not locate __version__ in shepherd/__init__.py")
    init.write_text(new_text)

    shutil.copy(REPO / "README.md", STAGE / "README.md")
    shutil.copy(REPO / "LICENSE", STAGE / "LICENSE")

    wheel_packages = "\n".join(f'    "src/{imp}",' for imp in PACKAGES)
    (STAGE / "pyproject.toml").write_text(
        PYPROJECT
        % {
            "version": version,
            "entry_points": ENTRY_POINTS,
            "wheel_packages": wheel_packages,
        }
    )


def main() -> int:
    """Stage the bundle and build sdist + wheel into --out-dir."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", default=DEFAULT_VERSION)
    ap.add_argument("--out-dir", default=str(REPO / "dist"), help="artifact output directory")
    args = ap.parse_args()

    stage(args.version)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("shepherd_ai-*"):
        f.unlink()

    result = subprocess.run(["uv", "build", "--out-dir", str(out)], cwd=STAGE, check=False)
    if result.returncode != 0:
        return result.returncode

    print("\nArtifacts:")
    for f in sorted(out.glob("shepherd_ai-*")):
        print(f"  {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
