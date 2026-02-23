"""Tests for git operations module."""

import os
import subprocess
import pytest
from unittest.mock import patch, MagicMock

from bugalizer.git_ops.repo import (
    clone_repo,
    get_head_sha,
    list_files,
    pull_repo,
    _validate_url,
)


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

def test_validate_url_https():
    """HTTPS URLs are accepted."""
    _validate_url("https://github.com/user/repo.git")


def test_validate_url_git_ssh():
    """git@ SSH URLs are accepted."""
    _validate_url("git@github.com:user/repo.git")


def test_validate_url_invalid():
    """Non-https/git@ URLs are rejected."""
    with pytest.raises(ValueError, match="Invalid repo URL"):
        _validate_url("http://example.com/repo.git")


def test_validate_url_ftp():
    """FTP URLs are rejected."""
    with pytest.raises(ValueError, match="Invalid repo URL"):
        _validate_url("ftp://example.com/repo.git")


# ---------------------------------------------------------------------------
# clone_repo
# ---------------------------------------------------------------------------

def test_clone_repo_new(tmp_path):
    """Clone into a new directory creates the repo."""
    target = str(tmp_path / "myrepo")

    with patch("bugalizer.git_ops.repo._run_git") as mock_git:
        mock_git.return_value = MagicMock(returncode=0, stderr="")
        result = clone_repo("https://github.com/test/repo.git", target)

    assert result == target
    mock_git.assert_called_once_with(
        ["clone", "--branch", "main", "--single-branch", "https://github.com/test/repo.git", target]
    )


def test_clone_repo_existing(tmp_path):
    """Clone into an existing repo directory does a pull instead."""
    target = tmp_path / "myrepo"
    target.mkdir()
    (target / ".git").mkdir()

    with patch("bugalizer.git_ops.repo._run_git") as mock_git:
        mock_git.return_value = MagicMock(returncode=0, stderr="")
        result = clone_repo("https://github.com/test/repo.git", str(target))

    assert result == str(target)
    mock_git.assert_called_once_with(["pull"], cwd=str(target))


def test_clone_repo_invalid_url(tmp_path):
    """Invalid URL raises ValueError."""
    with pytest.raises(ValueError, match="Invalid repo URL"):
        clone_repo("http://bad-url.com/repo", str(tmp_path / "repo"))


def test_clone_repo_failure(tmp_path):
    """Git clone failure raises RuntimeError."""
    target = str(tmp_path / "myrepo")

    with patch("bugalizer.git_ops.repo._run_git") as mock_git:
        mock_git.return_value = MagicMock(returncode=128, stderr="fatal: repo not found")
        with pytest.raises(RuntimeError, match="git clone failed"):
            clone_repo("https://github.com/test/repo.git", target)


# ---------------------------------------------------------------------------
# pull_repo
# ---------------------------------------------------------------------------

def test_pull_repo_success(tmp_path):
    """Successful pull does not raise."""
    with patch("bugalizer.git_ops.repo._run_git") as mock_git:
        mock_git.return_value = MagicMock(returncode=0, stderr="")
        pull_repo(str(tmp_path))

    mock_git.assert_called_once_with(["pull"], cwd=str(tmp_path))


def test_pull_repo_failure(tmp_path):
    """Git pull failure raises RuntimeError."""
    with patch("bugalizer.git_ops.repo._run_git") as mock_git:
        mock_git.return_value = MagicMock(returncode=1, stderr="error: merge conflict")
        with pytest.raises(RuntimeError, match="git pull failed"):
            pull_repo(str(tmp_path))


# ---------------------------------------------------------------------------
# get_head_sha
# ---------------------------------------------------------------------------

def test_get_head_sha(tmp_path):
    """Returns stripped SHA from git rev-parse."""
    with patch("bugalizer.git_ops.repo._run_git") as mock_git:
        mock_git.return_value = MagicMock(returncode=0, stdout="abc123def456\n", stderr="")
        sha = get_head_sha(str(tmp_path))

    assert sha == "abc123def456"


def test_get_head_sha_failure(tmp_path):
    """git rev-parse failure raises RuntimeError."""
    with patch("bugalizer.git_ops.repo._run_git") as mock_git:
        mock_git.return_value = MagicMock(returncode=128, stderr="fatal: not a repo")
        with pytest.raises(RuntimeError, match="git rev-parse failed"):
            get_head_sha(str(tmp_path))


# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------

def test_list_files_all(tmp_path):
    """Lists all tracked files without extension filter."""
    with patch("bugalizer.git_ops.repo._run_git") as mock_git:
        mock_git.return_value = MagicMock(
            returncode=0,
            stdout="src/main.py\nsrc/utils.py\nREADME.md\n",
            stderr="",
        )
        files = list_files(str(tmp_path))

    assert files == ["src/main.py", "src/utils.py", "README.md"]


def test_list_files_with_extension_filter(tmp_path):
    """Filters by extension when specified."""
    with patch("bugalizer.git_ops.repo._run_git") as mock_git:
        mock_git.return_value = MagicMock(
            returncode=0,
            stdout="src/main.py\nsrc/utils.py\nREADME.md\n",
            stderr="",
        )
        files = list_files(str(tmp_path), extensions=[".py"])

    assert files == ["src/main.py", "src/utils.py"]


def test_list_files_skips_vendor_dirs(tmp_path):
    """Vendor/generated directories are skipped."""
    with patch("bugalizer.git_ops.repo._run_git") as mock_git:
        mock_git.return_value = MagicMock(
            returncode=0,
            stdout="src/main.py\nnode_modules/pkg/index.js\nvendor/lib.go\n__pycache__/mod.pyc\n",
            stderr="",
        )
        files = list_files(str(tmp_path))

    assert files == ["src/main.py"]


def test_list_files_failure(tmp_path):
    """git ls-files failure raises RuntimeError."""
    with patch("bugalizer.git_ops.repo._run_git") as mock_git:
        mock_git.return_value = MagicMock(returncode=1, stderr="error")
        with pytest.raises(RuntimeError, match="git ls-files failed"):
            list_files(str(tmp_path))
