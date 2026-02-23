"""AST-based repo map builder using tree-sitter."""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from bugalizer.config import settings
from bugalizer.git_ops.repo import get_head_sha, list_files

logger = logging.getLogger(__name__)

# Supported language extensions and their tree-sitter language names.
LANGUAGE_MAP: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
}

SUPPORTED_EXTENSIONS = list(LANGUAGE_MAP.keys())


@dataclass
class FileSymbols:
    """Symbols extracted from a single file."""
    path: str
    classes: list[dict[str, Any]] = field(default_factory=list)
    functions: list[dict[str, Any]] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)


@dataclass
class RepoMap:
    """Complete repo map with ranked files."""
    project_id: str
    branch: str
    sha: str
    files: list[dict[str, Any]]
    text: str
    built_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "branch": self.branch,
            "sha": self.sha,
            "files": self.files,
            "text": self.text,
            "built_at": self.built_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RepoMap:
        return cls(
            project_id=data["project_id"],
            branch=data["branch"],
            sha=data["sha"],
            files=data["files"],
            text=data["text"],
            built_at=data["built_at"],
        )


def _get_language(ext: str):
    """Load a tree-sitter language for the given extension.

    Returns the Language object or None if unavailable.
    """
    lang_name = LANGUAGE_MAP.get(ext)
    if not lang_name:
        return None

    try:
        import tree_sitter

        if lang_name == "python":
            import tree_sitter_python as tsl
        elif lang_name == "javascript":
            import tree_sitter_javascript as tsl
        elif lang_name == "typescript":
            import tree_sitter_typescript as tsl_mod
            # tree-sitter-typescript exposes typescript and tsx
            return tree_sitter.Language(tsl_mod.language_typescript())
        elif lang_name == "tsx":
            import tree_sitter_typescript as tsl_mod
            return tree_sitter.Language(tsl_mod.language_tsx())
        elif lang_name == "go":
            import tree_sitter_go as tsl
        elif lang_name == "rust":
            import tree_sitter_rust as tsl
        elif lang_name == "java":
            import tree_sitter_java as tsl
        else:
            return None

        return tree_sitter.Language(tsl.language())
    except (ImportError, Exception) as e:
        logger.debug("tree-sitter language %s unavailable: %s", lang_name, e)
        return None


def _extract_symbols(source: bytes, language) -> FileSymbols:
    """Parse source with tree-sitter and extract symbols."""
    import tree_sitter

    parser = tree_sitter.Parser(language)
    tree = parser.parse(source)
    root = tree.root_node

    symbols = FileSymbols(path="")
    _walk_node(root, symbols)
    return symbols


def _walk_node(node, symbols: FileSymbols, class_name: str | None = None) -> None:
    """Recursively walk AST nodes and extract symbols."""
    for child in node.children:
        node_type = child.type

        # Python: function_definition, class_definition
        # JS/TS: function_declaration, class_declaration
        # Go: function_declaration, method_declaration
        # Rust: function_item, impl_item
        # Java: method_declaration, class_declaration

        if node_type in (
            "function_definition", "function_declaration", "function_item",
            "method_declaration", "method_definition",
        ):
            name = _get_name(child)
            if name:
                entry = {
                    "name": name,
                    "line": child.start_point[0] + 1,
                    "params": _count_params(child),
                }
                if class_name:
                    entry["class"] = class_name
                symbols.functions.append(entry)

        elif node_type in ("class_definition", "class_declaration", "impl_item"):
            name = _get_name(child)
            if name:
                methods = []
                # Recurse into class body to find methods
                cls_symbols = FileSymbols(path="")
                _walk_node(child, cls_symbols, class_name=name)
                methods = [f["name"] for f in cls_symbols.functions]
                symbols.classes.append({
                    "name": name,
                    "line": child.start_point[0] + 1,
                    "methods": methods,
                })
                # Also add methods to the top-level functions list
                symbols.functions.extend(cls_symbols.functions)

        elif node_type in ("import_statement", "import_from_statement",
                           "import_declaration"):
            text = child.text.decode("utf-8", errors="replace") if child.text else ""
            if text:
                symbols.imports.append(text)

        else:
            # Recurse into other nodes
            _walk_node(child, symbols, class_name=class_name)


def _get_name(node) -> str | None:
    """Extract the name from a function/class node."""
    for child in node.children:
        if child.type in ("identifier", "name", "type_identifier"):
            return child.text.decode("utf-8", errors="replace") if child.text else None
    return None


def _count_params(node) -> int:
    """Count parameters in a function node."""
    for child in node.children:
        if child.type in ("parameters", "formal_parameters", "parameter_list"):
            # Count identifier children (excluding self/this)
            count = 0
            for param in child.children:
                if param.type in ("identifier", "parameter", "typed_parameter",
                                  "default_parameter", "typed_default_parameter",
                                  "formal_parameter"):
                    name = param.text.decode("utf-8", errors="replace") if param.text else ""
                    if name not in ("self", "cls", "this"):
                        count += 1
            return count
    return 0


def _build_import_graph(all_symbols: dict[str, FileSymbols]) -> dict[str, int]:
    """Build import graph and return in-degree counts per file."""
    in_degree: dict[str, int] = defaultdict(int)

    # Map module names to file paths (simplified)
    module_to_file: dict[str, str] = {}
    for path in all_symbols:
        # Convert file path to a module-like name
        stem = path.replace("/", ".").replace("\\", ".")
        if stem.endswith(".py"):
            stem = stem[:-3]
        elif stem.endswith((".js", ".ts", ".tsx")):
            stem = stem.rsplit(".", 1)[0]
        module_to_file[stem] = path
        # Also map by basename
        basename = os.path.basename(path).rsplit(".", 1)[0]
        if basename not in module_to_file:
            module_to_file[basename] = path

    for path, symbols in all_symbols.items():
        for imp in symbols.imports:
            # Try to match imports to known files
            for module_name, target_path in module_to_file.items():
                if target_path != path and module_name in imp:
                    in_degree[target_path] += 1

    return dict(in_degree)


def _format_compact(ranked_files: list[dict[str, Any]], max_tokens: int) -> str:
    """Format ranked files into a compact text representation."""
    lines: list[str] = []
    estimated_chars = 0
    char_budget = max_tokens * 4  # ~4 chars per token

    for entry in ranked_files:
        path = entry["path"]
        rank = entry["rank"]
        header = f"{path} (rank: {rank})"

        if estimated_chars + len(header) + 1 > char_budget:
            break

        lines.append(header)
        estimated_chars += len(header) + 1

        for cls in entry.get("classes", []):
            cls_line = f"  class {cls['name']}"
            if estimated_chars + len(cls_line) + 1 > char_budget:
                break
            lines.append(cls_line)
            estimated_chars += len(cls_line) + 1

            for method in cls.get("methods", []):
                method_line = f"    def {method}()"
                if estimated_chars + len(method_line) + 1 > char_budget:
                    break
                lines.append(method_line)
                estimated_chars += len(method_line) + 1

        for fn in entry.get("functions", []):
            if fn.get("class"):
                continue  # Already shown under class
            fn_line = f"  def {fn['name']}()"
            if estimated_chars + len(fn_line) + 1 > char_budget:
                break
            lines.append(fn_line)
            estimated_chars += len(fn_line) + 1

    return "\n".join(lines)


def build_repo_map(
    project_id: str,
    branch: str,
    repo_path: str,
    *,
    max_files: int | None = None,
    max_tokens: int | None = None,
) -> RepoMap:
    """Build a repo map by parsing source files with tree-sitter.

    This is a blocking/CPU-bound function. Call via asyncio.to_thread()
    from async contexts.
    """
    if max_files is None:
        max_files = settings.repo_map_max_files
    if max_tokens is None:
        max_tokens = settings.repo_map_max_tokens

    sha = get_head_sha(repo_path)
    source_files = list_files(repo_path, extensions=SUPPORTED_EXTENSIONS)
    logger.info("Building repo map for %s: %d source files", project_id, len(source_files))

    all_symbols: dict[str, FileSymbols] = {}

    for fpath in source_files:
        full_path = os.path.join(repo_path, fpath)
        ext = os.path.splitext(fpath)[1]

        try:
            with open(full_path, "rb") as f:
                source = f.read()
        except (OSError, IOError) as e:
            logger.debug("Could not read %s: %s", fpath, e)
            # Include in map but without symbols
            all_symbols[fpath] = FileSymbols(path=fpath)
            continue

        language = _get_language(ext)
        if language is None:
            # No tree-sitter support — include file name only
            all_symbols[fpath] = FileSymbols(path=fpath)
            continue

        try:
            symbols = _extract_symbols(source, language)
            symbols.path = fpath
            all_symbols[fpath] = symbols
        except Exception as e:
            logger.debug("Parse failed for %s: %s", fpath, e)
            all_symbols[fpath] = FileSymbols(path=fpath)

    # Build import graph and rank
    in_degree = _build_import_graph(all_symbols)

    ranked: list[dict[str, Any]] = []
    for fpath, symbols in all_symbols.items():
        symbol_count = len(symbols.classes) + len(symbols.functions)
        ranked.append({
            "path": fpath,
            "in_degree": in_degree.get(fpath, 0),
            "symbol_count": symbol_count,
            "classes": [{"name": c["name"], "line": c["line"], "methods": c["methods"]}
                        for c in symbols.classes],
            "functions": [{"name": f["name"], "line": f["line"], "params": f["params"],
                           "class": f.get("class")}
                          for f in symbols.functions],
        })

    # Sort by in-degree (desc), then symbol count (desc)
    ranked.sort(key=lambda x: (x["in_degree"], x["symbol_count"]), reverse=True)

    # Trim to max_files
    ranked = ranked[:max_files]

    # Assign ranks
    for i, entry in enumerate(ranked, 1):
        entry["rank"] = i

    text = _format_compact(ranked, max_tokens)

    return RepoMap(
        project_id=project_id,
        branch=branch,
        sha=sha,
        files=ranked,
        text=text,
        built_at=time.time(),
    )


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class RepoMapCache:
    """File-system cache for repo maps."""

    def __init__(self, cache_dir: str | None = None):
        if cache_dir is None:
            cache_dir = os.path.join(settings.cache_dir, "repo_maps")
        self._cache_dir = cache_dir

    def _cache_path(self, project_id: str, branch: str, sha: str) -> Path:
        return Path(self._cache_dir) / project_id / branch / f"{sha}.json"

    def get(self, project_id: str, branch: str, sha: str) -> RepoMap | None:
        """Get a cached repo map, or None if not found/expired."""
        path = self._cache_path(project_id, branch, sha)
        if not path.exists():
            return None

        try:
            data = json.loads(path.read_text())
            built_at = data.get("built_at", 0)
            ttl_seconds = settings.repo_map_ttl_hours * 3600
            if time.time() - built_at > ttl_seconds:
                logger.debug("Cache expired for %s/%s/%s", project_id, branch, sha)
                return None
            return RepoMap.from_dict(data)
        except (json.JSONDecodeError, KeyError) as e:
            logger.debug("Cache read failed: %s", e)
            return None

    def put(self, repo_map: RepoMap) -> None:
        """Write a repo map to cache."""
        path = self._cache_path(repo_map.project_id, repo_map.branch, repo_map.sha)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(repo_map.to_dict()))
        logger.debug("Cached repo map at %s", path)

    def get_or_build(
        self,
        project_id: str,
        branch: str,
        repo_path: str,
    ) -> RepoMap:
        """Get from cache or build a new repo map.

        This is a blocking function. Call via asyncio.to_thread()
        from async contexts.
        """
        sha = get_head_sha(repo_path)

        cached = self.get(project_id, branch, sha)
        if cached:
            logger.info("Repo map cache hit for %s/%s/%s", project_id, branch, sha)
            return cached

        logger.info("Repo map cache miss, building for %s/%s/%s", project_id, branch, sha)
        repo_map = build_repo_map(project_id, branch, repo_path)
        self.put(repo_map)
        return repo_map


# Module-level cache instance
_cache: RepoMapCache | None = None


def get_repo_map_cache() -> RepoMapCache:
    """Get or create the module-level RepoMapCache instance."""
    global _cache
    if _cache is None:
        _cache = RepoMapCache()
    return _cache
