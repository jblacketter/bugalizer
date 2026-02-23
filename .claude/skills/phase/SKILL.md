# Skill: /phase

Show project phase status and navigation for Bugalizer.

## Usage

| Command | Description |
|---------|-------------|
| `/phase` | Show status dashboard for all phases |
| `/phase [name]` | Show details for a specific phase |

## Behavior

### `/phase` (no arguments)

Read these files and build a status dashboard:
1. `CLAUDE.md` — Implementation Status section
2. `docs/phases/` — List all phase plan files
3. `docs/handoffs/` — List all cycle files, check their STATE
4. `handoff-state.json` — Current active cycle

Output format:
```
Phase Status Dashboard
======================
Phase 1 (Foundation)        COMPLETE   30 tests
Phase 2 (Local LLM Pipeline) COMPLETE   66 tests
Phase 3 (Codebase Analysis) COMPLETE   113 tests
Phase 4 (Fix Proposals)     NOT STARTED
Phase 5 (Dashboard)         NOT STARTED
Phase 6 (Integrations)      NOT STARTED

Active: [phase name] | [plan/impl] | Round [N] | [status]
```

### `/phase [name]` (with argument)

Show details for the named phase:
1. Read `docs/phases/{name}.md` — Summary, scope, files
2. Read any cycle files in `docs/handoffs/{name}_*.md` — Review history
3. Report: plan status, impl status, key files created/modified, test count

## Phase Reference

| Phase | Plan File | Key Deliverables |
|-------|-----------|-----------------|
| architecture | `docs/phases/architecture.md` | Overall design, DB schema, API structure |
| local_llm_pipeline | `docs/phases/local_llm_pipeline.md` | Ollama triage, queue worker, retry logic |
| codebase_analysis | `docs/phases/codebase_analysis.md` | Git ops, repo maps, localization |
| fix_proposals | Not yet created | Cloud LLM, fix generation, PRs |
| dashboard | Not yet created | Web UI for monitoring |
| integrations | Not yet created | Webhooks, external systems |
