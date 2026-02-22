# Phase: Architecture — Bugalizer

## Summary

Design and implement an AI-powered bug report processing server ("Bugalizer") that accepts structured bug reports, queues them, pre-processes with local LLMs (Ollama), optionally escalates to cloud LLMs (Anthropic), and proposes automated fixes by analyzing the target project's codebase. Humans review the queue and approve/reject proposed fixes.

## Context & Research

### Existing Infrastructure

**qaagent** (Python, FastAPI + React dashboard, running at `http://192.168.68.56:8080/`):
- Already has LLM integration via litellm (Ollama local, Anthropic, OpenAI)
- Has session-based auth, repo management, agent config endpoints
- Token usage tracking and cost estimation built in
- RAG indexing for code analysis (chunked retrieval)
- React + Vite + Tailwind dashboard

**sonicgrid PR #233** (merged):
- Added in-app bug reporting: submit, fetch, resolve, reopen
- `bug_reports` table: id, reporter_id, reporter_name, reporter_email, description, status, attachments (JSONB), timestamps
- Simple workflow: active ↔ resolved
- This will be a future source of bug reports for Bugalizer

### Industry Research

**Automated bug fixing tools (SWE-agent, AutoCodeRover, Agentless, Aider):**
- **Agentless** (best cost/performance): 3-phase pipeline — Localize → Repair → Validate. ~$0.34/issue, 50.8% success rate on SWE-bench. No agent overhead. Used by OpenAI to benchmark models.
- **SWE-agent**: Takes GitHub issues, creates agent with shell access. More capable but more expensive.
- **AutoCodeRover**: Combines LLMs with program analysis. ~$0.70/issue. Good at AST-level localization.
- **Aider**: Repository map using graph-based file importance ranking. Default 1k token budget for repo map. Excellent token efficiency.

**Bug triage with local LLMs:**
- openSUSE Hack Week project: Ollama + Bugzilla integration for AI triage
- trIAge: AI assistant for issue quality control, categorization, duplicate detection, priority detection
- Local models (Llama 3.x, Qwen 2.5) can classify severity, correlate events, generate summaries

**Token efficiency best practices:**
- Aider's repo map: Graph-ranked file selection, ~1k tokens for project context
- LLMLingua: Prompt compression up to 20x
- Route simple tasks to smaller/cheaper models, reserve expensive models for complex reasoning
- Pre-process with regex/AST analysis before touching LLMs at all

**Bug lifecycle best practices (Jira, Azure DevOps, industry standard):**
- Core states: New → Triaged → In Progress → Fixed → Verified → Closed
- Resolution types: Fixed, Won't Fix, Duplicate, Cannot Reproduce, Deferred
- Track time-in-state for workflow optimization
- Triage stage critical for deciding which bugs need fixing

### OpenClaw (Future Experiment)
- User plans to experiment with OpenClaw on isolated Windows/WSL/Docker sandbox
- Recent CVEs (CVE-2026-25253) — host-level code execution in unpatched versions
- Security recommendation: dedicated VM, non-privileged credentials, non-sensitive data only
- Could be a future executor for bug fix proposals, but NOT for initial implementation

## Architecture Decision: Separate Service in Bugalizer Repo

**Decision:** Build Bugalizer as a standalone Python/FastAPI service in this repo, designed to integrate with qaagent's infrastructure but runnable independently.

**Rationale:**
1. qaagent is mature and complex — adding a bug processing pipeline directly would increase coupling
2. Bugalizer has a distinct domain (bug lifecycle management vs QA automation)
3. Standalone service can accept reports from any source (sonicgrid, manual, API, future apps)
4. Can reuse qaagent's litellm pattern for LLM integration without depending on it
5. Can share the same Windows machine and Ollama instance
6. Future integration: qaagent could submit discovered issues to Bugalizer

## Bug Report Schema (Required Fields)

```yaml
bug_report:
  required_at_submit:         # API rejects without these (HTTP 422)
    - title: string           # Short descriptive title
    - description: string     # Detailed bug description
    - reporter: string        # User identifier / email
    - project_id: string      # Which project/repo this is for
  recommended:                # API accepts without these but returns warnings
    - steps_to_reproduce: string[]  # Ordered steps
    - expected_behavior: string
    - actual_behavior: string
  optional:                   # No warnings if missing
    - url: string             # Page URL where bug occurred
    - feature_area: string    # Feature/module affected
    - severity: enum          # critical, high, medium, low
    - environment: string     # Browser, OS, device info
    - attachments: file[]     # Screenshots, logs
    - labels: string[]        # User-provided tags
```

**Validation strategy (two-tier):**
- **Hard required** (`title`, `description`, `reporter`, `project_id`): API returns 422 if missing. DB columns are `NOT NULL`.
- **Recommended** (`steps_to_reproduce`, `expected_behavior`, `actual_behavior`): API accepts the report but includes a `warnings` array in the response listing missing recommended fields with suggestion text (e.g., "Adding steps to reproduce helps AI analyze the bug faster"). DB columns are nullable. The AI triage phase (Phase 2) may set status to `clarification_needed` if these are absent and the description alone is insufficient.
- **Optional**: No validation, no warnings. DB columns are nullable.

## Bug Status Workflow

### Canonical Status Set (13 states)

| Status | Owner | Description | Terminal? |
|--------|-------|-------------|-----------|
| `submitted` | system | Report received, awaiting validation | No |
| `validating` | system | Automated validation in progress | No |
| `triaged` | system/human | Valid report, queued for AI analysis | No |
| `analyzing` | AI | AI pipeline is actively processing | No |
| `clarification_needed` | human | AI needs more info from reporter | No |
| `fix_proposed` | AI | AI generated a candidate fix | No |
| `fix_approved` | human | Human approved the proposed fix | No |
| `fix_committed` | system | Branch created, fix committed/PR opened | No |
| `verified` | human | Fix confirmed working | No |
| `closed` | human/system | Bug resolved and done | **Yes** |
| `rejected` | human/system | Invalid or unreproducible report | **Yes** |
| `duplicate` | human/AI | Matches an existing report | **Yes** |
| `deferred` | human | Valid but parked for later (re-openable) | No |

Terminal states: `closed`, `rejected`, `duplicate`. These cannot transition to other states (except `closed` → `submitted` via explicit reopen).

`deferred` is non-terminal: it can transition back to `triaged` when a human re-prioritizes.

### Transition Rules

```
submitted → validating          (automatic, on submit)
submitted → rejected            (human: invalid report)
submitted → triaged             (human: skip validation, manual triage)

validating → triaged            (validation passed)
validating → rejected           (validation failed: missing required data)

triaged → analyzing             (AI pipeline picks up)
triaged → deferred              (human: park for later)
triaged → duplicate             (human or AI: matches existing)
triaged → closed                (human: won't fix / not a bug)

analyzing → clarification_needed (AI: insufficient info)
analyzing → fix_proposed         (AI: generated a fix)
analyzing → triaged              (AI: analysis failed, return to queue)

clarification_needed → analyzing (reporter provides info, re-analyze)
clarification_needed → closed    (reporter abandons / no response timeout)

fix_proposed → fix_approved      (human approves)
fix_proposed → triaged           (human rejects fix, return to queue)
fix_proposed → closed            (human: fix not viable, close bug)

fix_approved → fix_committed     (system: branch + commit created)

fix_committed → verified         (human: confirmed working)
fix_committed → triaged          (human: fix didn't work, retry)

verified → closed                (automatic or human: done)

deferred → triaged               (human: re-prioritize)
```

## Tiered LLM Pipeline (Agentless-inspired)

### Phase 1: Validation & Pre-processing (No LLM)
- Schema validation (required fields check)
- Duplicate detection (fuzzy text matching against existing reports)
- Extract structured data: URLs, file paths, error messages, stack traces
- Cost: $0

### Phase 2: Triage & Classification (Local LLM — Ollama)
- Severity classification
- Category assignment (UI, API, data, auth, performance, etc.)
- Feature area identification
- Generate structured summary
- Identify if enough info exists for analysis or if clarification needed
- Model: qwen2.5-coder:7b (already configured in qaagent) or codestral
- Cost: $0 (local GPU)

### Phase 3: Codebase Analysis & Localization (Local LLM + Repo Map)
- Build Aider-style repo map: AST-parsed file graph, ranked by relevance
- Localize: Identify candidate files/functions related to the bug
- Cross-reference bug description with code structure
- Model: Larger local model (qwen2.5-coder:32b or deepseek-coder-v2) if GPU supports it
- Cost: $0 (local GPU)

### Phase 4: Fix Proposal (Cloud LLM — Anthropic, optional)
- Only triggered for bugs where Phase 3 produced confident localization
- Send focused context (repo map + localized files + bug report) to Anthropic
- Ask for: root cause analysis, proposed fix (diff), confidence score
- Token budget: ~4k context (repo map) + ~8k (localized code) + ~2k (bug report) = ~14k input
- Model: claude-sonnet-4-5 (good balance of cost/capability) via litellm
- Estimated cost: ~$0.04-0.15 per bug (Sonnet pricing)
- Fallback: Skip this phase, present Phase 3 analysis only

### Token Efficiency Strategy
1. **Pre-filter before LLM**: Regex/AST analysis extracts structured data at zero cost
2. **Repo map compression**: Graph-ranked file list keeps context under 4k tokens
3. **Focused localization**: Only send relevant files to cloud LLM, not entire codebase
4. **Tiered escalation**: Most bugs handled by free local LLM; only complex ones hit paid API
5. **Caching**: Cache repo maps per project with deterministic invalidation (see below)
6. **Prompt templates**: Structured prompts avoid wasted tokens on instruction repetition

### Repo Map Cache Invalidation

**Cache key**: `{project_id}:{branch}:{HEAD_commit_sha}`

**Storage**: Repo maps cached as JSON files in `{data_dir}/cache/repo_maps/{project_id}/{branch}/{sha}.json`

**Invalidation rules:**
- **On bug report submission**: Check `git rev-parse HEAD` of the project's default branch. If SHA differs from cached map's SHA → rebuild.
- **On explicit refresh**: `POST /api/v1/projects/{id}/refresh-map` triggers a git pull + rebuild regardless of cache state.
- **Periodic background**: Optional configurable interval (default: disabled). When enabled, a background task runs `git fetch` every N minutes and rebuilds if remote HEAD has advanced.
- **TTL**: Cached maps expire after 24 hours even if SHA hasn't changed (safety net for edge cases like force-pushes that reuse a SHA).

**Build cost**: Repo map build is pure AST parsing (tree-sitter), no LLM involved. Typical build time: <5 seconds for a 10k-file repo. Cached maps are reused across all bug reports for the same project+branch+SHA.

## Security Model for Credentials

### API Keys (Bugalizer's own auth)
- Stored in environment variable `BUGALIZER_API_KEYS` (comma-separated list)
- Validated via `X-API-Key` header on every request
- No DB storage of Bugalizer API keys — config/env only

### LLM Provider API Keys (Anthropic, OpenAI, etc.)
- **Storage**: Encrypted in SQLite `projects.api_key_encrypted` column using Fernet symmetric encryption (from `cryptography` library)
- **Encryption key**: Stored in environment variable `BUGALIZER_SECRET_KEY` (32-byte base64 Fernet key)
- **Local dev**: Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and set in `.env`
- **Production**: Set via host environment or secrets manager
- **Rotation**: Re-encrypt all stored keys when `BUGALIZER_SECRET_KEY` changes. Provide a CLI command: `bugalizer rotate-keys --old-key <old> --new-key <new>`
- **Fallback**: If no per-project key is stored, fall back to `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` environment variables (same pattern as qaagent)

### Ollama (Local LLM)
- No API key needed — communicates over local HTTP (`http://localhost:11434`)
- Ollama host configurable via `OLLAMA_HOST` env var for non-default setups

### Git Credentials (for repo cloning/pushing)
- Uses the host's existing git credential configuration (SSH keys, credential helpers)
- Bugalizer does NOT store git credentials — relies on system-level auth
- For private repos: user must have SSH key or credential helper configured on the host

## Technical Stack

```
Server:        Python 3.11+ / FastAPI / Uvicorn
Database:      SQLite (simple, no external deps — same as qaagent)
Queue:         In-process asyncio task queue (start simple, upgrade to Redis/Celery if needed)
LLM (local):   Ollama via litellm (reuse qaagent pattern)
LLM (cloud):   Anthropic via litellm (reuse qaagent pattern)
Auth:          API key + optional session auth (JWT)
Frontend:      React + Vite + Tailwind (can share qaagent's component patterns)
Git:           GitPython for repo operations (clone, branch, diff)
Code Analysis: tree-sitter for AST parsing (multi-language repo maps)
```

## Project Structure

```
bugalizer/
  src/
    bugalizer/
      __init__.py
      main.py                 # FastAPI app entry point
      config.py               # Settings (Pydantic BaseSettings)
      db.py                   # SQLite database layer
      models.py               # Pydantic models (BugReport, Project, etc.)
      auth.py                 # API key / session auth
      api/
        __init__.py
        reports.py            # Bug report CRUD endpoints
        projects.py           # Project/repo management
        queue.py              # Queue status and management
        webhooks.py           # Incoming webhooks (sonicgrid, etc.)
      pipeline/
        __init__.py
        validator.py          # Phase 1: Schema validation, dedup
        triage.py             # Phase 2: LLM triage & classification
        localizer.py          # Phase 3: Codebase analysis & localization
        fixer.py              # Phase 4: Fix proposal generation
        repo_map.py           # Aider-style repo map builder
        orchestrator.py       # Pipeline coordinator
      llm/
        __init__.py
        client.py             # litellm wrapper (from qaagent pattern)
        prompts.py            # Prompt templates
      git_ops/
        __init__.py
        repo.py               # Git operations (clone, branch, diff, PR)
      queue/
        __init__.py
        worker.py             # Async queue worker
        models.py             # Queue job models
  dashboard/                  # React frontend (Phase 2)
  tests/
  pyproject.toml
  requirements.txt
```

## Database Schema

```sql
-- Projects registered for bug analysis
CREATE TABLE projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  repo_url TEXT NOT NULL,         -- Git clone URL
  repo_path TEXT,                 -- Local clone path
  default_branch TEXT DEFAULT 'main',
  llm_provider TEXT DEFAULT 'ollama',
  llm_model TEXT DEFAULT 'qwen2.5-coder:7b',
  api_key_encrypted TEXT,         -- For cloud LLM (encrypted at rest)
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Bug reports
CREATE TABLE bug_reports (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  steps_to_reproduce TEXT,        -- JSON array
  expected_behavior TEXT,
  actual_behavior TEXT,
  reporter TEXT NOT NULL,
  url TEXT,
  feature_area TEXT,
  severity TEXT DEFAULT 'medium',
  environment TEXT,
  attachments TEXT,               -- JSON array of file refs
  labels TEXT,                    -- JSON array
  status TEXT NOT NULL DEFAULT 'submitted',
  resolution_reason TEXT,          -- e.g. 'fixed', 'wont_fix', 'not_a_bug', 'cannot_reproduce' (set when entering terminal state)
  assigned_to TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Analysis results from the pipeline
CREATE TABLE analyses (
  id TEXT PRIMARY KEY,
  bug_report_id TEXT NOT NULL REFERENCES bug_reports(id),
  phase TEXT NOT NULL,            -- 'validation', 'triage', 'localization', 'fix_proposal'
  status TEXT NOT NULL,           -- 'pending', 'running', 'completed', 'failed'
  result TEXT,                    -- JSON: phase-specific output
  llm_provider TEXT,
  llm_model TEXT,
  prompt_tokens INTEGER DEFAULT 0,
  completion_tokens INTEGER DEFAULT 0,
  estimated_cost_usd REAL DEFAULT 0.0,
  started_at TEXT,
  completed_at TEXT,
  created_at TEXT NOT NULL
);

-- Proposed fixes
CREATE TABLE fix_proposals (
  id TEXT PRIMARY KEY,
  bug_report_id TEXT NOT NULL REFERENCES bug_reports(id),
  analysis_id TEXT REFERENCES analyses(id),
  branch_name TEXT,
  diff TEXT,                      -- The proposed fix as unified diff
  explanation TEXT,               -- AI explanation of the fix
  confidence REAL,                -- 0.0 - 1.0
  root_cause TEXT,                -- AI's root cause analysis
  files_changed TEXT,             -- JSON array
  status TEXT DEFAULT 'proposed', -- proposed, approved, rejected, committed
  reviewed_by TEXT,
  review_notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Token usage tracking (per project)
CREATE TABLE token_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL REFERENCES projects(id),
  bug_report_id TEXT REFERENCES bug_reports(id),
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_tokens INTEGER DEFAULT 0,
  completion_tokens INTEGER DEFAULT 0,
  estimated_cost_usd REAL DEFAULT 0.0,
  created_at TEXT NOT NULL
);

CREATE INDEX idx_bug_reports_project ON bug_reports(project_id);
CREATE INDEX idx_bug_reports_status ON bug_reports(status);
CREATE INDEX idx_analyses_bug_report ON analyses(bug_report_id);
CREATE INDEX idx_fix_proposals_bug_report ON fix_proposals(bug_report_id);
CREATE INDEX idx_token_usage_project ON token_usage(project_id);
```

## API Endpoints (v1)

```
POST   /api/v1/reports              # Submit a bug report
GET    /api/v1/reports              # List reports (filterable by status, project)
GET    /api/v1/reports/{id}         # Get report detail + analysis + proposals
PATCH  /api/v1/reports/{id}/status  # Update status (human actions)
DELETE /api/v1/reports/{id}         # Delete report

POST   /api/v1/projects             # Register a project/repo
GET    /api/v1/projects             # List projects
GET    /api/v1/projects/{id}        # Project detail
PATCH  /api/v1/projects/{id}        # Update project settings
DELETE /api/v1/projects/{id}        # Remove project

GET    /api/v1/queue                # Queue overview (counts by status)
POST   /api/v1/queue/{id}/retry     # Retry failed analysis
POST   /api/v1/queue/{id}/escalate  # Manually escalate to cloud LLM

GET    /api/v1/proposals/{id}       # Get fix proposal detail
POST   /api/v1/proposals/{id}/approve   # Approve fix
POST   /api/v1/proposals/{id}/reject    # Reject fix
POST   /api/v1/proposals/{id}/commit    # Create branch + commit fix

POST   /api/v1/webhooks/sonicgrid   # Webhook for sonicgrid bug reports
POST   /api/v1/webhooks/generic     # Generic webhook (configurable schema mapping)

GET    /api/v1/usage                # Token usage summary
GET    /api/v1/usage/{project_id}   # Per-project usage

GET    /health                      # Health check
```

## Phased Implementation Plan

### Phase 1: Foundation (This PR)

**What is fully working at the end of Phase 1:**

Endpoints delivered:
- `POST /api/v1/reports` — Submit bug report with full schema validation (returns 422 with field-level guidance on missing required fields)
- `GET /api/v1/reports` — List reports, filterable by `status` and `project_id` query params
- `GET /api/v1/reports/{id}` — Full report detail (no analysis/proposals yet)
- `PATCH /api/v1/reports/{id}/status` — Manual status transitions (human-driven only)
- `DELETE /api/v1/reports/{id}` — Soft delete
- `POST /api/v1/projects` — Register a project (name, repo_url, default_branch)
- `GET /api/v1/projects` / `GET /api/v1/projects/{id}` — List/detail
- `PATCH /api/v1/projects/{id}` — Update project settings
- `GET /api/v1/queue` — Queue overview (counts by status, no processing yet)
- `GET /health` — Health check

Persisted fields (SQLite):
- Full `projects` table (all columns except `api_key_encrypted` which is Phase 4)
- Full `bug_reports` table (all columns)
- `analyses` and `fix_proposals` tables created but empty (schema only)

Status transitions executable in Phase 1 code (manual/human only):
- `submitted` → `triaged` (human marks as valid)
- `submitted` → `rejected` (human marks as invalid)
- `triaged` → `deferred` / `duplicate` / `closed` (human triage decisions; `closed` with `resolution_reason` e.g. "wont_fix", "not_a_bug", "cannot_reproduce")
- `deferred` → `triaged` (human re-prioritizes)
- Any non-terminal → `closed` (human closes)

NOT in Phase 1 (deferred to later phases):
- No LLM calls (no Ollama, no Anthropic)
- No async queue processing (reports sit in `submitted` until manually triaged)
- No automated `validating` → `triaged` transition (Phase 2)
- No `analyzing` / `fix_proposed` / `fix_approved` / `fix_committed` transitions (Phases 2-4)
- No git operations, repo cloning, or repo maps (Phase 3)
- No dashboard UI (Phase 5)
- No webhooks (Phase 6)

Auth in Phase 1:
- API key auth via `X-API-Key` header (keys stored in config/env, not DB)
- No session auth or JWT (deferred to Phase 5 with dashboard)

### Phase 2: Local LLM Pipeline
- Ollama integration via litellm
- Phase 1 pipeline: Validation + pre-processing
- Phase 2 pipeline: Triage & classification
- Async queue worker
- Token usage tracking

### Phase 3: Codebase Analysis
- Git repo cloning and management
- AST-based repo map builder (tree-sitter)
- Phase 3 pipeline: Localization
- File relevance scoring

### Phase 4: Fix Proposals
- Anthropic integration via litellm
- Phase 4 pipeline: Fix generation
- Diff generation and preview
- Branch creation and commit
- Proposal review workflow

### Phase 5: Dashboard
- React frontend (queue view, report detail, proposals)
- Real-time status updates (WebSocket)
- Usage/cost dashboard

### Phase 6: Integrations
- Sonicgrid webhook integration
- Generic webhook support
- GitHub issue sync (optional)
- qaagent integration (discovered issues → Bugalizer)

## Success Criteria

1. A running FastAPI server that accepts structured bug reports via REST API
2. Validates required fields and provides guidance for missing info
3. Queues reports and processes them through the tiered LLM pipeline
4. Local Ollama handles triage and initial analysis at zero token cost
5. Cloud LLM (Anthropic) used only for confident fix proposals, keeping costs low
6. Human review interface for approving/rejecting proposed fixes
7. Full status tracking from submission through resolution
8. Token usage tracking with cost estimates
9. Designed for future integration with sonicgrid and other bug sources
