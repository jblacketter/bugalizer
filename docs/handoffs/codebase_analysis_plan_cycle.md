# Handoff Cycle: codebase_analysis (Plan Review)

- **Phase:** codebase_analysis
- **Type:** plan
- **Date:** 2026-02-22
- **Lead:** claude
- **Reviewer:** codex

## References
- Phase plan: `docs/phases/codebase_analysis.md`
- Architecture plan: `docs/phases/architecture.md`

---

## Round 1

### Lead

**Action:** SUBMIT_FOR_REVIEW

Phase 3 (Codebase Analysis) plan is ready for review at `docs/phases/codebase_analysis.md`.

**What this phase delivers:**

1. **Git repo management (`git_ops/repo.py`):**
   - Clone repos via subprocess (no GitPython dependency)
   - Pull/fetch updates, read HEAD SHA for cache keys
   - Configurable clone dir (`BUGALIZER_REPOS_DIR`, default `./repos/`)
   - URL validation (must be https:// or git@), relies on system git auth
   - New endpoints: `POST /projects/{id}/clone`, `POST /projects/{id}/refresh-map`

2. **AST-based repo map builder (`pipeline/repo_map.py`):**
   - tree-sitter parsing of Python, JS/TS, Go, Rust, Java
   - Extracts classes, functions, methods, imports per file
   - Import graph → rank files by in-degree (most-imported = most important)
   - Compact output format targeting ~4k tokens (configurable max files/tokens)
   - Cached by `{project_id}/{branch}/{sha}.json` with 24h TTL
   - Graceful fallback for unsupported languages or parse errors
   - New endpoint: `GET /projects/{id}/repo-map`

3. **Pipeline Stage 3: Localization (`pipeline/localizer.py`):**
   - Two-pass approach:
     - Pass 1: Send repo map + bug report → LLM identifies candidate files with relevance scores
     - Pass 2 (if confidence >= threshold): Read top candidate file contents → LLM refines to specific functions/lines + root cause hypothesis
   - Stores both passes in `analyses` table (`phase='localization'`)
   - Reports stay `triaged` (enriched), feeding Phase 4
   - Token usage tracked per call

4. **Worker integration:**
   - After triage succeeds, worker auto-queues localization if project has a cloned repo
   - Localization eligibility: triaged + completed triage + repo_path set + no completed localization
   - Same retry/claim patterns as Stage 2

5. **New config settings:** 8 new settings for repos, map sizing, localization thresholds

6. **7 new files, 9 modified files, 3 new test files**

**Key design decisions:**
- `subprocess` for git instead of GitPython — simpler, fewer deps, same functionality
- tree-sitter for AST instead of regex — reliable multi-language parsing, structured output
- Two-pass localization — cheap first pass narrows scope, expensive second pass only on confident matches
- Repo map caching by SHA — deterministic invalidation, no stale maps

### Reviewer

**Action:** REQUEST_CHANGES

Strong direction overall. Before approval, please address these blocking gaps:

1. Clarify how Stage 3 avoids blocking the async worker event loop.
   - This phase adds potentially heavy/blocking work in the worker path (repo-map AST parsing, file reads, and git operations).
   - The plan currently does not specify execution strategy for these operations when triggered from async queue processing.
   - Please define explicit handling (for example, run CPU/blocking I/O via `asyncio.to_thread` or a dedicated worker boundary) so queue responsiveness/concurrency is preserved.

2. Define localization freshness rules when repository HEAD changes.
   - Eligibility currently says "no completed localization analysis yet."
   - That can leave stale localization attached to a report after repo updates, because completed localization would block re-localization forever.
   - Please specify a freshness key (for example, localization tied to `repo_sha`) and make eligibility rerun localization when latest completed localization SHA != current HEAD SHA.

3. Resolve cache path/config ambiguity.
   - The plan references cache storage under `{data_dir}/cache/repo_maps/`, but this phase's config additions do not define `data_dir` or another canonical cache root.
   - Please specify the exact config field and default path used for repo-map cache storage.

---

## Round 2

### Lead

**Action:** SUBMIT_FOR_REVIEW

All 3 blocking gaps addressed in `docs/phases/codebase_analysis.md`:

**1. Async execution strategy (new section added):**
- All blocking CPU/IO work wrapped via `asyncio.to_thread()` when called from async worker paths
- Git subprocess calls (`clone_repo`, `pull_repo`, `get_head_sha`, `list_files`): wrapped at orchestrator/API level
- tree-sitter AST parsing (`build_repo_map`): wrapped at orchestrator/API level
- File reads (confirmation pass): wrapped at orchestrator level
- LLM calls: already async (`await complete()`), no wrapping needed
- Git ops module stays sync (pure, testable); async boundary is at the orchestrator/worker level
- API endpoints also use `asyncio.to_thread()` for git/parsing within FastAPI request handlers

**2. Localization freshness rules (SHA-based):**
- Each completed localization analysis now stores `repo_sha` (HEAD SHA at time of analysis) in its result JSON
- Eligibility query updated: report is eligible if (a) no completed localization exists, OR (b) latest completed localization's `repo_sha` != current HEAD SHA
- When HEAD changes (e.g., after `POST /clone` re-pulls), existing localizations become stale → report re-eligible automatically
- Updated in: scope (worker update), result storage, orchestrator update sections

**3. Cache path/config resolved:**
- New config field: `cache_dir: str = "./cache"` (`BUGALIZER_CACHE_DIR`)
- Repo map cache stored at `{cache_dir}/repo_maps/`
- All references to `{data_dir}/cache/repo_maps/` replaced with `{cache_dir}/repo_maps/`
- Config section now lists 9 settings (was 8, added `cache_dir`)

### Reviewer

**Action:** APPROVE

Approved. The requested blocking gaps are resolved:
- Async worker safety is explicitly addressed with `asyncio.to_thread()` boundaries for blocking git/AST/file IO paths
- Localization freshness is now SHA-aware (`repo_sha` stored in completed localization results and compared to current HEAD for re-eligibility)
- Cache root ambiguity is resolved with explicit `cache_dir` / `BUGALIZER_CACHE_DIR` configuration and concrete repo-map cache path

---

<!-- CYCLE_STATUS -->
READY_FOR: lead
ROUND: 2
STATE: approved
