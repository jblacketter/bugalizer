# Skill: /test

Run and analyze tests for the Bugalizer project.

## Usage

| Command | Description |
|---------|-------------|
| `/test` | Run full test suite |
| `/test [pattern]` | Run tests matching a pattern (file, class, or function name) |

## Behavior

1. **No arguments**: Run `python -m pytest -q` for the full suite. Report pass/fail count.

2. **With pattern argument**: Determine what to run:
   - If pattern matches a test file (e.g., `localizer`, `queue`, `api`), run `python -m pytest tests/test_{pattern}.py -v`
   - If pattern looks like a test function (e.g., `test_clone_repo`), run `python -m pytest -k "{pattern}" -v`
   - If pattern is `--last-failed` or `lf`, run `python -m pytest --lf -v`

3. **After running**: Report results concisely:
   - Total passed/failed/errors
   - For failures: show the test name and the assertion or error message (not the full traceback)
   - If all pass, just show the count and time

## Project Test Structure

```
tests/
  test_api.py       # API endpoints, auth, phase gating (30 tests)
  test_pipeline.py  # Validation, triage, orchestrator (19 tests)
  test_queue.py     # Queue eligibility, retries, db locking (11 tests)
  test_usage.py     # Usage endpoints, retry endpoint (6 tests)
  test_git_ops.py   # Git operations (15 tests)
  test_repo_map.py  # Repo map builder and cache (11 tests)
  test_localizer.py # Localization, eligibility, path safety (21 tests)
```

## Important Notes
- Tests use in-memory SQLite (`BUGALIZER_DB_PATH=:memory:`)
- Queue worker is disabled in tests (`BUGALIZER_QUEUE_ENABLED=false`)
- LLM calls are mocked (no Ollama dependency needed)
- Run from project root: `C:\Users\jblac\projects\bugalizer`
