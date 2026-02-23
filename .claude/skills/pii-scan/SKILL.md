# Skill: /pii-scan

Scan Bugalizer codebase for PII handling risks. Traces how personal data flows through the system and identifies exposure points.

## Usage

| Command | Description |
|---------|-------------|
| `/pii-scan` | Full PII audit of the codebase |
| `/pii-scan [area]` | Scan a specific area: `api`, `pipeline`, `db`, `llm`, `logs` |

## Behavior

Scan the codebase using Grep, Glob, and Read tools. Do NOT modify files. Report all findings.

### 1. Data Ingestion Points
Scan API endpoints that accept external data:
- `src/bugalizer/api/reports.py` — Bug report creation (title, description, reporter email, steps, URLs)
- `src/bugalizer/api/projects.py` — Project config (repo URLs, potentially credentials)
- Identify all fields that could contain PII: reporter names/emails, free-text descriptions, URLs with query params, environment info

### 2. Data Storage
- Scan `src/bugalizer/db.py` schema for PII-bearing columns
- Check encryption: is report content encrypted at rest?
- Check for data retention: are there auto-purge mechanisms?
- Check `analyses.result` — LLM triage/localization results stored as plaintext JSON that may echo PII from reports

### 3. LLM Prompt Exposure
This is the highest-risk area. Scan:
- `src/bugalizer/llm/prompts.py` — Are report fields (description, steps, reporter) sent directly to LLMs?
- `src/bugalizer/pipeline/triage.py` — Does the full report text go to Ollama?
- `src/bugalizer/pipeline/localizer.py` — Does bug report content go to the localization LLM?
- Check if any PII redaction/scrubbing happens before LLM calls
- Note: Local Ollama is lower risk than cloud APIs, but still stores data in model context

### 4. Logging Exposure
- Grep for `logger.info`, `logger.debug`, `logger.error`, `logger.warning` that log report content
- Check if error messages include PII (e.g., `f"Failed for report {report}"` logging full report dict)
- Check if LLM responses containing PII are logged

### 5. API Response Exposure
- Check if internal analysis data (which may contain PII) is exposed via API responses
- Check if error responses leak PII
- Check CORS settings (currently `allow_origins=["*"]`)

### 6. File System Exposure
- Check if cloned repos could contain PII (repo contents read in localization)
- Check if cache files (`cache/repo_maps/`) could contain PII
- Check file permissions on repos_dir and cache_dir

## Risk Categories

Classify each finding:
- **CRITICAL**: PII sent to external/cloud service without redaction, PII in logs at INFO level, PII in unencrypted backups
- **HIGH**: PII stored in plaintext in DB without retention policy, PII in error responses to API clients
- **MEDIUM**: PII in debug-level logs, PII in local LLM prompts (Ollama), overly broad CORS
- **LOW**: PII in memory during processing (expected), PII in internal data structures

## Output Format

```
PII Scan Report
===============
Data Flow: API Input -> DB Storage -> LLM Prompts -> Analysis Results -> API Output

CRITICAL Findings:
  [C1] Reporter email stored in plaintext (db.py, bug_reports.reporter)
  [C2] Full report description sent to LLM without redaction (triage.py:57)

HIGH Findings:
  [H1] No data retention/purge mechanism for old reports
  [H2] Analysis results echo PII from reports (analyses.result JSON)

MEDIUM Findings:
  [M1] CORS allows all origins (main.py:44)

Recommendations:
  1. Add PII redaction layer before LLM calls
  2. Add data retention policy and auto-purge
  3. Encrypt report content at rest
  4. Restrict CORS to known dashboard origins
```

## Regulatory Context

When scanning, consider requirements from:
- **CCPA** — California Consumer Privacy Act (relevant for US auto dealerships)
- **GLBA** — Gramm-Leach-Bliley Act (if financial data involved)
- **State breach notification laws** — If PII is compromised
- **GDPR** — If any EU customers are involved

Flag any findings where the current implementation would fail these requirements.
