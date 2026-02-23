# Phase: Codebase Analysis

## Summary

Add git repository management and AST-based codebase analysis to the pipeline. Projects get their repos cloned locally, parsed into Aider-style repo maps via tree-sitter, and when a triaged bug report enters Stage 3, the localizer uses the repo map + LLM to identify candidate files and functions related to the bug. Results are stored as localization analyses, feeding Phase 4 (fix proposals).

## Scope

### In Scope
- Git repo cloning and management (`git_ops/`)
  - Clone project repos to a configurable local directory
  - Pull/fetch to update existing clones
  - Read HEAD commit SHA for cache invalidation
  - Set `repo_path` on the project record after cloning
- AST-based repo map builder (`pipeline/repo_map.py`)
  - Parse source files using tree-sitter to extract: classes, functions, methods, imports
  - Build a graph-ranked file list (Aider-style) sorted by importance/connectivity
  - Output a compact repo map (target: under 4k tokens for context window efficiency)
  - Support Python, JavaScript/TypeScript, Go, Rust, Java (common languages)
  - Cache repo maps keyed by `{project_id}:{branch}:{HEAD_sha}`
  - Cache storage: JSON files in `{cache_dir}/repo_maps/` (where `cache_dir` is `BUGALIZER_CACHE_DIR`, default `./cache/`)
  - Invalidation: SHA mismatch, explicit refresh, or 24h TTL
- Pipeline Stage 3: Localization (`pipeline/localizer.py`)
  - Takes a triaged+enriched report (post-Stage 2)
  - Sends repo map + bug report to LLM
  - LLM identifies: candidate files, candidate functions, relevance scores, reasoning
  - Reads candidate file contents and sends focused context for confirmation
  - Stores result in `analyses` table with `phase='localization'`
  - **Status outcome**: stays `triaged` (enriched with localization data, ready for Phase 4)
  - On failure: analysis saved as failed, report stays triaged for retry
- Localization prompt templates (`llm/prompts.py`)
  - `LOCALIZE_SYSTEM_PROMPT` — Instructs LLM to identify relevant code locations
  - `LOCALIZE_USER_TEMPLATE` — Formats repo map + bug report for analysis
  - `LOCALIZE_CONFIRM_TEMPLATE` — Sends candidate file contents for confirmation/refinement
- API endpoints
  - `POST /api/v1/projects/{id}/clone` — Trigger repo clone/update
  - `POST /api/v1/projects/{id}/refresh-map` — Force repo map rebuild
  - `GET /api/v1/projects/{id}/repo-map` — View current repo map
  - `GET /api/v1/reports/{id}/localization` — View localization results for a report
- Queue worker update
  - After Stage 2 triage completes successfully, worker also runs Stage 3 if the project has a cloned repo
  - Localization eligibility: triaged report with completed triage analysis, project has `repo_path` set, AND either (a) no completed localization analysis exists, or (b) the latest completed localization's `repo_sha` differs from the current HEAD SHA (stale localization)
  - Each completed localization analysis stores the `repo_sha` it was built against in its result JSON
  - When HEAD changes (e.g., after `POST /clone` re-pulls), existing localizations become stale and the report is re-eligible
- Phase gating update
  - No new states to unlock (localization stays in `triaged` → `analyzing` → `triaged`)
  - But update orchestrator to chain Stage 3 after Stage 2

### Out of Scope (Later Phases)
- Fix proposal generation (Phase 4)
- Cloud LLM / Anthropic calls (Phase 4)
- Branch creation, diffs, PRs (Phase 4)
- Dashboard UI (Phase 5)
- Webhooks / integrations (Phase 6)

## Technical Approach

### Git Operations (`src/bugalizer/git_ops/repo.py`)

Uses `subprocess` to run git commands (avoids GitPython dependency, simpler and more predictable):
- `clone_repo(url, target_dir, branch='main') -> str` — Clones repo, returns path. If already exists, does `git pull` instead.
- `get_head_sha(repo_path) -> str` — Returns `git rev-parse HEAD`
- `pull_repo(repo_path) -> None` — Runs `git pull` on existing clone
- `list_files(repo_path, extensions=None) -> list[str]` — Lists tracked files, optionally filtered by extension

Clone directory: `BUGALIZER_REPOS_DIR` (default: `./repos/`). Each project cloned to `{repos_dir}/{project_id}/`.

Security considerations:
- Only clone from URLs configured by admin via project settings (not user-provided at report time)
- Validate repo_url format before cloning (must start with `https://` or `git@`)
- No credential storage — relies on system-level git auth (SSH keys, credential helpers)

### Repo Map Builder (`src/bugalizer/pipeline/repo_map.py`)

Aider-inspired approach, simplified for Phase 3:

**Step 1: File discovery**
- List all tracked files via `git ls-files`
- Filter to supported languages by extension: `.py`, `.js`, `.ts`, `.tsx`, `.go`, `.rs`, `.java`
- Skip vendor/generated dirs: `node_modules/`, `vendor/`, `.git/`, `__pycache__/`, `dist/`, `build/`

**Step 2: AST parsing**
- Use tree-sitter to parse each file and extract symbols:
  - Functions/methods: name, line number, parameter count
  - Classes: name, line number, method names
  - Imports: what module/file is imported
- Build a symbol table per file

**Step 3: Graph ranking**
- Build an import graph: file A imports from file B → edge A→B
- Rank files by in-degree (most-imported files = most important)
- Secondary sort by symbol count (files with more definitions = more central)

**Step 4: Compact output**
- Format as a condensed text representation:
  ```
  src/app/main.py (rank: 1)
    class App
      def __init__(self, config)
      def run(self)
    def create_app() -> App
  src/app/models.py (rank: 2)
    class User
      def validate(self) -> bool
    class Project
  ...
  ```
- Target: top N files by rank, fitting within ~4k tokens
- `BUGALIZER_REPO_MAP_MAX_FILES` (default: 50) — max files included in map
- `BUGALIZER_REPO_MAP_MAX_TOKENS` (default: 4000) — approximate token budget (estimate 4 chars/token)

**Caching:**
- `RepoMapCache` class manages cache directory (`{settings.cache_dir}/repo_maps/`)
- `get_or_build(project_id, branch, repo_path) -> RepoMap` — checks cache, builds if stale
- Cache key: `{project_id}/{branch}/{sha}.json`
- TTL: 24 hours (configurable via `BUGALIZER_REPO_MAP_TTL_HOURS`, default: 24)

**tree-sitter integration:**
- Use `tree-sitter` Python bindings with pre-built language grammars
- Language grammars installed as pip packages: `tree-sitter-python`, `tree-sitter-javascript`, etc.
- Graceful fallback: if tree-sitter fails to parse a file (unsupported language, syntax error), include the file name in the map but skip symbols

### Pipeline Localizer (`src/bugalizer/pipeline/localizer.py`)

Stage 3 — Local LLM + repo map:

**Step 1: Initial localization**
- Build/retrieve repo map for the project
- Send repo map + bug report (title, description, triage summary) to LLM
- Request JSON response:
  ```json
  {
    "candidate_files": [
      {"path": "src/foo.py", "relevance": 0.9, "reason": "..."},
      {"path": "src/bar.py", "relevance": 0.7, "reason": "..."}
    ],
    "confidence": 0.0-1.0
  }
  ```
- Model: project's configured `llm_model` (or `BUGALIZER_DEFAULT_LOCALIZE_MODEL`, default same as triage model)

**Step 2: Confirmation pass (optional, if confidence >= threshold)**
- Read the top-N candidate file contents (max `BUGALIZER_LOCALIZE_MAX_FILE_CHARS`, default: 8000 chars per file, max 3 files)
- Send file contents + bug report to LLM for refined localization
- Request refined JSON with specific function/class targets:
  ```json
  {
    "localizations": [
      {"file": "src/foo.py", "function": "handle_submit", "line_range": [42, 67], "confidence": 0.85, "reason": "..."}
    ],
    "root_cause_hypothesis": "string"
  }
  ```

**Result storage:**
- Stored in `analyses` table: `phase='localization'`, result contains both initial and confirmation pass data plus `repo_sha` (the HEAD SHA at time of analysis)
- `repo_sha` enables freshness checks: if HEAD changes, the localization is stale and the report becomes re-eligible
- Token usage logged per LLM call
- Report stays `triaged` (enriched), ready for Phase 4

### Async Execution Strategy

Stage 3 introduces blocking CPU/IO work (git subprocess calls, tree-sitter AST parsing, file reads) that must not block the async worker event loop:

- **Git operations** (`clone_repo`, `pull_repo`, `get_head_sha`, `list_files`): All use `subprocess.run()` which blocks. Wrapped via `asyncio.to_thread()` when called from async worker paths.
- **AST parsing** (`build_repo_map`): CPU-bound tree-sitter parsing of potentially many files. Wrapped via `asyncio.to_thread()` when called from the worker.
- **File reads** (confirmation pass reading candidate file contents): Disk I/O, wrapped via `asyncio.to_thread()`.
- **LLM calls**: Already async (uses `await complete()`), no wrapping needed.

The orchestrator's `process_localization()` function calls these operations through async wrappers:
```python
async def process_localization(report_id: str) -> None:
    # ... claim report ...
    sha = await asyncio.to_thread(get_head_sha, repo_path)
    repo_map = await asyncio.to_thread(get_or_build, project_id, branch, repo_path)
    # LLM call is already async
    result = await localize_pass1(repo_map, report)
    if result.confidence >= threshold:
        file_contents = await asyncio.to_thread(read_candidate_files, ...)
        result = await localize_pass2(file_contents, report)
    # ... store results ...
```

The git operations module exposes sync functions only. The async boundary is at the orchestrator/worker level — sync functions are pure and testable without async, while `asyncio.to_thread()` handles the event-loop integration.

API endpoints (`POST /clone`, `POST /refresh-map`) also use `asyncio.to_thread()` for git/parsing calls since they run inside the async FastAPI request handler.

### Orchestrator Update (`src/bugalizer/pipeline/orchestrator.py`)

- New function: `process_localization(report_id: str) -> None`
  - Claims report (triaged → analyzing) for localization
  - Checks project has repo_path (skip if not cloned)
  - Gets current HEAD SHA via `asyncio.to_thread(get_head_sha, ...)`
  - Builds/retrieves repo map via `asyncio.to_thread(get_or_build, ...)`
  - Runs localizer (LLM calls are already async; file reads in confirmation pass use `asyncio.to_thread`)
  - Stores result with `repo_sha` for freshness tracking
  - Returns to triaged on completion or failure
- Worker integration: after triage completes, if project has repo, auto-queue localization
- Localization eligibility query checks `repo_sha` in latest completed localization against current HEAD — re-runs if stale

### Config Updates (`src/bugalizer/config.py`)

New settings:
- `repos_dir: str = "./repos"` — directory for cloned repos
- `cache_dir: str = "./cache"` — root directory for all file-based caches (repo maps stored under `{cache_dir}/repo_maps/`)
- `default_localize_model: str = "qwen2.5-coder:7b"` — model for localization
- `repo_map_max_files: int = 50` — max files in repo map
- `repo_map_max_tokens: int = 4000` — approximate token budget for map
- `repo_map_ttl_hours: int = 24` — cache TTL
- `localize_max_file_chars: int = 8000` — max chars per file in confirmation pass
- `localize_max_files: int = 3` — max files to read for confirmation
- `localize_confidence_threshold: float = 0.5` — minimum confidence for confirmation pass

## Files

### New Files
| File | Purpose |
|------|---------|
| `src/bugalizer/git_ops/__init__.py` | Package init |
| `src/bugalizer/git_ops/repo.py` | Git clone, pull, SHA, file listing |
| `src/bugalizer/pipeline/repo_map.py` | AST-based repo map builder + cache |
| `src/bugalizer/pipeline/localizer.py` | Stage 3: LLM-based code localization |
| `tests/test_git_ops.py` | Git operations tests (using temp repos) |
| `tests/test_repo_map.py` | Repo map builder tests (with sample files) |
| `tests/test_localizer.py` | Localization tests (mocked LLM) |

### Modified Files
| File | Changes |
|------|---------|
| `src/bugalizer/config.py` | Add repos_dir, localization, repo_map settings |
| `src/bugalizer/models.py` | Add RepoMap, Localization response models |
| `src/bugalizer/db.py` | Add localization eligibility query, repo map cache helpers |
| `src/bugalizer/llm/prompts.py` | Add localization prompt templates |
| `src/bugalizer/pipeline/orchestrator.py` | Add `process_localization()`, chain after triage |
| `src/bugalizer/queue/worker.py` | Add localization dispatch after triage |
| `src/bugalizer/api/projects.py` | Add clone, refresh-map, repo-map endpoints |
| `src/bugalizer/api/reports.py` | Add localization results endpoint |
| `pyproject.toml` | Add tree-sitter dependencies |

## Success Criteria

1. `POST /api/v1/projects/{id}/clone` clones the project's repo to `repos_dir` and sets `repo_path`
2. Repo maps are built via tree-sitter AST parsing and cached by project+branch+SHA
3. `GET /api/v1/projects/{id}/repo-map` returns the current repo map
4. `POST /api/v1/projects/{id}/refresh-map` forces a rebuild (git pull + re-parse)
5. After triage, reports for projects with cloned repos are automatically localized
6. Localization identifies candidate files with relevance scores and reasoning
7. Confirmation pass reads file contents and refines to specific functions/lines
8. All LLM calls log token usage
9. `GET /api/v1/reports/{id}/localization` returns localization results
10. All existing tests (66) continue to pass
11. New tests cover: git ops, repo map building/caching, localization with mocked LLM

## Dependencies

- `tree-sitter>=0.21` — AST parsing
- `tree-sitter-python`, `tree-sitter-javascript`, `tree-sitter-typescript`, `tree-sitter-go`, `tree-sitter-rust`, `tree-sitter-java` — language grammars
- Git installed on the host system
- For tests: temp git repos created in fixtures, LLM calls mocked
