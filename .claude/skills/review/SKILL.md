# Skill: /review

Pre-submission code review checklist for Bugalizer. Run before `/handoff` to catch common issues.

## Usage

| Command | Description |
|---------|-------------|
| `/review` | Run full review checklist on recent changes |
| `/review [file]` | Review a specific file |

## Behavior

Run through this checklist and report findings. Use Glob, Grep, and Read tools — do NOT modify any files.

### 1. Test Coverage
- Check if any new `.py` files in `src/bugalizer/` lack a corresponding test file in `tests/`
- Check if new public functions/classes have test coverage
- Run `python -m pytest -q` to verify all tests pass

### 2. Import Health
- Check for circular imports: scan import statements in new/modified files
- Check for unused imports
- Check that `__init__.py` files exist for new packages

### 3. Security Scan (Quick)
- Grep for hardcoded secrets, API keys, passwords in source files
- Check `subprocess` calls for unsanitized input
- Check file operations for path traversal (paths joined with user/external input)
- Check SQL queries for string formatting (should use parameterized queries)
- Check for `eval()`, `exec()`, `pickle.loads()` usage

### 4. Code Quality
- Check for TODO/FIXME/HACK comments left behind
- Check for `print()` statements (should use `logging`)
- Check for bare `except:` clauses (should catch specific exceptions)
- Check that new config settings have `BUGALIZER_` prefix convention

### 5. Documentation Sync
- Check if `CLAUDE.md` project structure matches actual file layout
- Check if test counts in `CLAUDE.md` match actual test count
- Check if implementation status is accurate

### 6. DB Consistency
- Check that schema changes in `db.py` have corresponding migrations in `_migrate()`
- Check that new DB functions used by the worker have `@retry_on_locked`

## Output Format

```
Pre-Review Checklist
====================
[PASS] Tests: 113 passed, 0 failed
[PASS] No circular imports detected
[WARN] Missing test coverage for src/bugalizer/foo.py
[FAIL] Hardcoded secret found in config.py:42
[PASS] No SQL injection patterns
...

Summary: X passed, Y warnings, Z failures
```

Flag items as PASS, WARN, or FAIL. Only FAIL items should block a handoff submission.
