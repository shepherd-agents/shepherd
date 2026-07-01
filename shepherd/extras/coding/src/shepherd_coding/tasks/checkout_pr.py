"""CheckoutPR task — clone or worktree a repository at a specific SHA.

Programmatic task (no LLM). Selects the fastest available strategy:
git worktree if a local .git exists, fresh clone otherwise.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field
from shepherd_runtime.task.authoring import Input, Output, task


def _has_local_repo(clone_url: str) -> Path | None:
    """Check if we already have a local clone of this repo.

    Returns the repo root path if found, None otherwise.
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        )
        local_url = result.stdout.strip().rstrip("/").removesuffix(".git")
        target_url = clone_url.rstrip("/").removesuffix(".git")
        if local_url == target_url:
            root = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=True,
            )
            return Path(root.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return None


@task
class CheckoutPR(BaseModel):
    """Checkout a repository at a PR's head commit.

    Programmatic task — uses git commands directly, no LLM involved.
    Selects worktree strategy when a local clone exists (3x faster),
    falls back to fresh clone otherwise.
    """

    head_sha: Input(str) = Field(description="SHA to checkout")
    clone_url: Input(str) = Field(description="Git HTTPS clone URL")
    workspace_dir: Input(str | None) = Field(default=None, description="Target dir (auto-created if None)")

    workspace_path: Output(str) = ""
    checked_out_sha: Output(str) = ""
    strategy: Output(str) = ""

    def execute(self) -> None:
        target = self.workspace_dir or tempfile.mkdtemp(prefix="pr-review-checkout-")

        local_repo = _has_local_repo(self.clone_url)

        if local_repo is not None:
            self._checkout_worktree(local_repo, target)
        else:
            self._checkout_clone(target)

        # Verify the checkout
        actual_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=target,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        self.workspace_path = target
        self.checked_out_sha = actual_sha

    def _checkout_worktree(self, repo_root: Path, target: str) -> None:
        """Fast path: create a git worktree (shares .git objects)."""
        # Fetch the SHA first to ensure it's available locally
        subprocess.run(
            ["git", "fetch", "origin", self.head_sha],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,  # May fail if SHA already local
        )
        subprocess.run(
            ["git", "worktree", "add", "--detach", target, self.head_sha],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        self.strategy = "worktree"

    def _checkout_clone(self, target: str) -> None:
        """Slow path: fresh clone for environments without a local repo."""
        subprocess.run(
            ["git", "clone", "--no-checkout", self.clone_url, target],
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["git", "checkout", self.head_sha],
            cwd=target,
            capture_output=True,
            text=True,
            check=True,
        )
        self.strategy = "clone"
