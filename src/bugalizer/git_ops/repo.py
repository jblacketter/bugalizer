"""Git repository management via subprocess."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_URL_PATTERN = re.compile(r"^(https://|git@)")


def _validate_url(url: str) -> None:
    """Validate repo URL starts with https:// or git@."""
    if not _URL_PATTERN.match(url):
        raise ValueError(f"Invalid repo URL (must start with https:// or git@): {url}")


def _run_git(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run a git command and return the result."""
    cmd = ["git"] + args
    logger.debug("git %s (cwd=%s)", " ".join(args), cwd)
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=300,
    )


def clone_repo(url: str, target_dir: str, branch: str = "main") -> str:
    """Clone a repo to target_dir. If already exists, pulls instead.

    Returns the repo path.
    """
    _validate_url(url)
    target = Path(target_dir)

    if target.exists() and (target / ".git").is_dir():
        logger.info("Repo already exists at %s, pulling", target_dir)
        pull_repo(target_dir)
        return str(target)

    target.parent.mkdir(parents=True, exist_ok=True)
    result = _run_git(["clone", "--branch", branch, "--single-branch", url, str(target)])
    if result.returncode != 0:
        raise RuntimeError(f"git clone failed: {result.stderr.strip()}")

    logger.info("Cloned %s to %s", url, target_dir)
    return str(target)


def pull_repo(repo_path: str) -> None:
    """Pull latest changes in an existing clone."""
    result = _run_git(["pull"], cwd=repo_path)
    if result.returncode != 0:
        raise RuntimeError(f"git pull failed: {result.stderr.strip()}")
    logger.info("Pulled updates in %s", repo_path)


def get_head_sha(repo_path: str) -> str:
    """Return the HEAD commit SHA."""
    result = _run_git(["rev-parse", "HEAD"], cwd=repo_path)
    if result.returncode != 0:
        raise RuntimeError(f"git rev-parse failed: {result.stderr.strip()}")
    return result.stdout.strip()


def list_files(repo_path: str, extensions: list[str] | None = None) -> list[str]:
    """List tracked files, optionally filtered by extension.

    Args:
        repo_path: Path to the git repository.
        extensions: Optional list of extensions to filter by (e.g. [".py", ".js"]).

    Returns:
        List of relative file paths.
    """
    result = _run_git(["ls-files"], cwd=repo_path)
    if result.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {result.stderr.strip()}")

    files = [f for f in result.stdout.strip().split("\n") if f]

    if extensions:
        ext_set = set(extensions)
        files = [f for f in files if os.path.splitext(f)[1] in ext_set]

    # Skip vendor/generated directories
    skip_dirs = {"node_modules/", "vendor/", ".git/", "__pycache__/", "dist/", "build/"}
    filtered = []
    for f in files:
        if not any(f.startswith(d) or f"/{d}" in f for d in skip_dirs):
            filtered.append(f)

    return filtered
