# Skill: /security-check

Security audit of the Bugalizer codebase. Checks for OWASP Top 10 vulnerabilities and common security issues.

## Usage

| Command | Description |
|---------|-------------|
| `/security-check` | Full security audit |
| `/security-check [category]` | Check specific category: `injection`, `auth`, `config`, `deps`, `crypto` |

## Behavior

Scan the codebase using Grep, Glob, and Read tools. Do NOT modify files. Report all findings with severity and remediation.

### 1. Injection (OWASP A03)

**SQL Injection:**
- Grep for f-string or `.format()` in SQL queries in `db.py`
- Verify all queries use parameterized `?` placeholders
- Check for any dynamic table/column names from user input

**Command Injection:**
- Scan `git_ops/repo.py` — all `subprocess` calls
- Check that repo URLs are validated before passing to `git clone`
- Check that file paths from external input are sanitized
- Look for `shell=True` in subprocess calls (dangerous)

**Path Traversal:**
- Scan file read/write operations for unsanitized paths
- Check `localizer.py` candidate file reading
- Check `repo_map.py` file reading
- Check `cache` directory operations
- Verify `_validate_candidate_path()` is applied everywhere files from external input are read

### 2. Authentication & Authorization (OWASP A01, A07)

- Check `auth.py` for bypass conditions
- Verify all API endpoints have `Depends(require_api_key)`
- Check if API keys are logged or exposed in responses
- Check for timing attacks in key comparison
- Verify `BUGALIZER_API_KEYS` empty = auth disabled (is this documented/intentional?)

### 3. Security Misconfiguration (OWASP A05)

- Check CORS settings in `main.py` (`allow_origins=["*"]` is overly permissive)
- Check debug mode settings
- Check if stack traces are exposed in production error responses
- Check for default credentials or secrets
- Verify `BUGALIZER_SECRET_KEY` is not hardcoded

### 4. Cryptographic Failures (OWASP A02)

- Check if sensitive data is encrypted at rest (API keys, report content)
- Check if `secret_key` config is used and how
- Verify no weak hashing (MD5, SHA1 for security purposes)
- Check TLS/HTTPS enforcement (or lack thereof)

### 5. Dependency Risks (OWASP A06)

- Read `pyproject.toml` — check for known-vulnerable version ranges
- Flag dependencies that have broad access: `litellm` (network), `subprocess` (OS)
- Check if dependency pins are too loose (e.g., `>=` without upper bound)

### 6. Logging & Monitoring (OWASP A09)

- Check if security events are logged (failed auth, invalid transitions, claim failures)
- Check if sensitive data appears in logs
- Verify log levels are appropriate (no PII at INFO, auth failures logged)

### 7. Server-Side Request Forgery (OWASP A10)

- Check `git clone` URL handling — can an attacker clone from `file://` or internal URLs?
- Check if LLM API base URL can be manipulated
- Verify URL validation in `git_ops/repo.py` blocks `file://`, `ftp://`, etc.

## Output Format

```
Security Audit Report
=====================

CRITICAL:
  [S1] Command injection risk: subprocess with unsanitized input (git_ops/repo.py:XX)

HIGH:
  [S2] CORS allows all origins — enables CSRF from any domain (main.py:44)
  [S3] Auth disabled when API_KEYS empty — no warning logged

MEDIUM:
  [S4] Git clone URL validation allows git@ but not verified against SSRF
  [S5] No rate limiting on API endpoints

LOW:
  [S6] Dependencies use >= without upper bounds

Remediation Priority:
  1. [S1] Validate all subprocess arguments against allowlist
  2. [S2] Restrict CORS origins to configured dashboard URL
  3. [S3] Log warning when auth is disabled
```

## Severity Definitions

- **CRITICAL**: Exploitable now, allows RCE/data exfiltration, must fix before production
- **HIGH**: Significant risk, exploitable with moderate effort, fix before external exposure
- **MEDIUM**: Defense-in-depth gap, limited exploitability, fix in next phase
- **LOW**: Best practice deviation, minimal risk, fix when convenient
