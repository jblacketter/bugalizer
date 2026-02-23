"""Tests for repo map builder and cache."""

import json
import os
import time
import pytest
from unittest.mock import patch, MagicMock

os.environ["BUGALIZER_DB_PATH"] = ":memory:"
os.environ["BUGALIZER_QUEUE_ENABLED"] = "false"

from bugalizer.pipeline.repo_map import (
    FileSymbols,
    RepoMap,
    RepoMapCache,
    build_repo_map,
    _build_import_graph,
    _format_compact,
    SUPPORTED_EXTENSIONS,
)


@pytest.fixture(autouse=True)
def reset_settings():
    from bugalizer.config import settings
    settings.repo_map_max_files = 50
    settings.repo_map_max_tokens = 4000
    settings.repo_map_ttl_hours = 24
    settings.cache_dir = "./cache"
    yield


# ---------------------------------------------------------------------------
# FileSymbols / RepoMap data classes
# ---------------------------------------------------------------------------

def test_repo_map_serialization():
    """RepoMap can round-trip through dict."""
    rm = RepoMap(
        project_id="proj1",
        branch="main",
        sha="abc123",
        files=[{"path": "main.py", "rank": 1}],
        text="main.py (rank: 1)",
        built_at=1000.0,
    )
    d = rm.to_dict()
    restored = RepoMap.from_dict(d)
    assert restored.project_id == "proj1"
    assert restored.sha == "abc123"
    assert restored.files == [{"path": "main.py", "rank": 1}]


# ---------------------------------------------------------------------------
# Import graph
# ---------------------------------------------------------------------------

def test_build_import_graph():
    """Files imported by others get higher in-degree."""
    symbols = {
        "src/main.py": FileSymbols(path="src/main.py", imports=["from utils import helper"]),
        "src/utils.py": FileSymbols(path="src/utils.py", imports=[]),
        "src/views.py": FileSymbols(path="src/views.py", imports=["import utils"]),
    }
    graph = _build_import_graph(symbols)
    # utils.py should have in-degree from both main.py and views.py
    assert graph.get("src/utils.py", 0) >= 1


# ---------------------------------------------------------------------------
# Compact format
# ---------------------------------------------------------------------------

def test_format_compact_basic():
    """Formats ranked files into text representation."""
    ranked = [
        {
            "path": "src/main.py",
            "rank": 1,
            "classes": [{"name": "App", "methods": ["run", "stop"]}],
            "functions": [
                {"name": "run", "class": "App"},
                {"name": "stop", "class": "App"},
                {"name": "main"},
            ],
        },
    ]
    text = _format_compact(ranked, max_tokens=4000)
    assert "src/main.py (rank: 1)" in text
    assert "class App" in text
    assert "def main()" in text


def test_format_compact_respects_budget():
    """Stops adding content when token budget is reached."""
    # Create many files to exceed a tiny budget
    ranked = [
        {"path": f"file_{i}.py", "rank": i, "classes": [], "functions": []}
        for i in range(100)
    ]
    text = _format_compact(ranked, max_tokens=10)  # Very small budget
    lines = text.strip().split("\n")
    assert len(lines) < 100  # Should not include all files


# ---------------------------------------------------------------------------
# build_repo_map (with mocked git + tree-sitter)
# ---------------------------------------------------------------------------

def test_build_repo_map_no_tree_sitter(tmp_path):
    """Build repo map gracefully handles missing tree-sitter."""
    # Create a sample Python file
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "app.py").write_text("def hello():\n    pass\n")

    with patch("bugalizer.pipeline.repo_map.get_head_sha", return_value="abc123"), \
         patch("bugalizer.pipeline.repo_map.list_files", return_value=["src/app.py"]), \
         patch("bugalizer.pipeline.repo_map._get_language", return_value=None):

        repo_map = build_repo_map("proj1", "main", str(tmp_path))

    assert repo_map.project_id == "proj1"
    assert repo_map.sha == "abc123"
    assert len(repo_map.files) == 1
    assert repo_map.files[0]["path"] == "src/app.py"


def test_build_repo_map_file_read_error(tmp_path):
    """Build repo map handles unreadable files gracefully."""
    with patch("bugalizer.pipeline.repo_map.get_head_sha", return_value="abc123"), \
         patch("bugalizer.pipeline.repo_map.list_files", return_value=["src/missing.py"]):

        repo_map = build_repo_map("proj1", "main", str(tmp_path))

    assert len(repo_map.files) == 1
    assert repo_map.files[0]["path"] == "src/missing.py"
    # File included but with no symbols
    assert repo_map.files[0]["symbol_count"] == 0


def test_build_repo_map_max_files(tmp_path):
    """Respects max_files limit."""
    files = [f"file_{i}.py" for i in range(20)]

    with patch("bugalizer.pipeline.repo_map.get_head_sha", return_value="abc123"), \
         patch("bugalizer.pipeline.repo_map.list_files", return_value=files), \
         patch("bugalizer.pipeline.repo_map._get_language", return_value=None):

        repo_map = build_repo_map("proj1", "main", str(tmp_path), max_files=5)

    assert len(repo_map.files) == 5


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def test_cache_put_and_get(tmp_path):
    """Cache stores and retrieves repo maps."""
    cache = RepoMapCache(cache_dir=str(tmp_path))
    rm = RepoMap(
        project_id="proj1",
        branch="main",
        sha="abc123",
        files=[],
        text="test",
        built_at=time.time(),
    )
    cache.put(rm)
    result = cache.get("proj1", "main", "abc123")
    assert result is not None
    assert result.sha == "abc123"


def test_cache_miss(tmp_path):
    """Cache returns None for missing entries."""
    cache = RepoMapCache(cache_dir=str(tmp_path))
    result = cache.get("proj1", "main", "missing")
    assert result is None


def test_cache_expired(tmp_path):
    """Cache returns None for expired entries."""
    cache = RepoMapCache(cache_dir=str(tmp_path))
    rm = RepoMap(
        project_id="proj1",
        branch="main",
        sha="abc123",
        files=[],
        text="test",
        built_at=time.time() - 100000,  # Very old
    )
    cache.put(rm)

    from bugalizer.config import settings
    settings.repo_map_ttl_hours = 1  # 1 hour TTL
    result = cache.get("proj1", "main", "abc123")
    assert result is None


def test_cache_get_or_build(tmp_path):
    """get_or_build builds on cache miss, returns cached on hit."""
    cache = RepoMapCache(cache_dir=str(tmp_path / "cache"))

    with patch("bugalizer.pipeline.repo_map.get_head_sha", return_value="abc123"), \
         patch("bugalizer.pipeline.repo_map.list_files", return_value=[]), \
         patch("bugalizer.pipeline.repo_map._get_language", return_value=None):

        # First call builds
        result1 = cache.get_or_build("proj1", "main", str(tmp_path))
        assert result1.sha == "abc123"

        # Second call hits cache
        result2 = cache.get_or_build("proj1", "main", str(tmp_path))
        assert result2.sha == "abc123"
