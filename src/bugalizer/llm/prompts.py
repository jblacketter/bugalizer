"""Prompt templates for pipeline stages."""

from __future__ import annotations

from typing import Any

TRIAGE_SYSTEM_PROMPT = """\
You are a bug triage specialist. Analyze bug reports and classify them.
Respond with ONLY a JSON object, no other text."""

TRIAGE_USER_TEMPLATE = """\
Analyze this bug report and provide a triage classification.

Title: {title}
Description: {description}
Steps to Reproduce: {steps_to_reproduce}
Expected Behavior: {expected_behavior}
Actual Behavior: {actual_behavior}
Reporter Severity: {severity}
Feature Area: {feature_area}
Environment: {environment}

Respond with a JSON object:
{{
  "severity": "critical|high|medium|low",
  "category": "ui|api|data|auth|performance|infrastructure|other",
  "feature_area": "string or null",
  "summary": "one-sentence summary of the bug",
  "needs_clarification": true or false,
  "clarification_questions": ["question1", "question2"],
  "confidence": 0.0 to 1.0
}}"""


def format_triage_prompt(report: dict) -> list[dict[str, str]]:
    """Format a bug report into triage prompt messages."""
    steps = report.get("steps_to_reproduce")
    if isinstance(steps, list):
        steps_text = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(steps))
    else:
        steps_text = steps or "Not provided"

    user_content = TRIAGE_USER_TEMPLATE.format(
        title=report.get("title", ""),
        description=report.get("description", ""),
        steps_to_reproduce=steps_text,
        expected_behavior=report.get("expected_behavior") or "Not provided",
        actual_behavior=report.get("actual_behavior") or "Not provided",
        severity=report.get("severity", "medium"),
        feature_area=report.get("feature_area") or "Not provided",
        environment=report.get("environment") or "Not provided",
    )

    return [
        {"role": "system", "content": TRIAGE_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


# ---------------------------------------------------------------------------
# Localization prompts (Stage 3)
# ---------------------------------------------------------------------------

LOCALIZE_SYSTEM_PROMPT = """\
You are a code localization specialist. Given a bug report and a repository map, \
identify which source files are most likely related to the bug.
Respond with ONLY a JSON object, no other text."""

LOCALIZE_USER_TEMPLATE = """\
Analyze this bug report and identify the most relevant source files from the repository.

## Bug Report
Title: {title}
Description: {description}
Severity: {severity}
Feature Area: {feature_area}
{triage_summary_section}
## Repository Map
{repo_map}

Respond with a JSON object:
{{
  "candidate_files": [
    {{"path": "src/example.py", "relevance": 0.9, "reason": "explanation"}},
    {{"path": "src/other.py", "relevance": 0.7, "reason": "explanation"}}
  ],
  "confidence": 0.0 to 1.0
}}"""

LOCALIZE_CONFIRM_TEMPLATE = """\
Review these source file contents and refine the bug localization to specific \
functions and line ranges.

## Bug Report
Title: {title}
Description: {description}
{triage_summary_section}
## Source Files
{file_contents}

Respond with a JSON object:
{{
  "localizations": [
    {{"file": "src/example.py", "function": "handle_submit", "line_range": [42, 67], "confidence": 0.85, "reason": "explanation"}}
  ],
  "root_cause_hypothesis": "brief description of the likely root cause"
}}"""


def format_localize_prompt(
    report: dict,
    repo_map_text: str,
    triage_summary: str | None = None,
) -> list[dict[str, str]]:
    """Format a bug report + repo map into localization prompt messages."""
    triage_section = ""
    if triage_summary:
        triage_section = f"\nTriage Summary: {triage_summary}\n"

    user_content = LOCALIZE_USER_TEMPLATE.format(
        title=report.get("title", ""),
        description=report.get("description", ""),
        severity=report.get("severity", "medium"),
        feature_area=report.get("feature_area") or "Not provided",
        triage_summary_section=triage_section,
        repo_map=repo_map_text,
    )

    return [
        {"role": "system", "content": LOCALIZE_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def format_localize_confirm_prompt(
    report: dict,
    file_contents: dict[str, str],
    triage_summary: str | None = None,
) -> list[dict[str, str]]:
    """Format file contents for the confirmation pass."""
    triage_section = ""
    if triage_summary:
        triage_section = f"\nTriage Summary: {triage_summary}\n"

    files_text = ""
    for path, content in file_contents.items():
        files_text += f"\n### {path}\n```\n{content}\n```\n"

    user_content = LOCALIZE_CONFIRM_TEMPLATE.format(
        title=report.get("title", ""),
        description=report.get("description", ""),
        triage_summary_section=triage_section,
        file_contents=files_text,
    )

    return [
        {"role": "system", "content": LOCALIZE_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


# ---------------------------------------------------------------------------
# Fix-proposal prompts (Stage 4 / bugalizer Phase 4)
# ---------------------------------------------------------------------------

FIX_PROPOSAL_SYSTEM_PROMPT = """\
You are an expert software engineer proposing a bug fix.

Given a bug report, a localization analysis identifying candidate source
files and functions, and the current contents of those files, produce a
fix proposal.

Respond with ONLY a JSON object, no prose, no code fences. The JSON must
match exactly this schema:

{
  "root_cause": "One-sentence explanation of the underlying defect.",
  "explanation": "Multi-sentence explanation of what the fix does and why.",
  "diff": "Unified diff (git format) of the proposed changes. Use a/ and b/ prefixes, include at least 3 context lines, and do not include file-creation stanzas unless adding a new file.",
  "confidence": 0.0,
  "files_changed": ["path/one.py", "path/two.py"]
}

Hard constraints:
- confidence must be a real float in [0.0, 1.0] that reflects your actual
  certainty. Do not default to 1.0.
- diff must be a non-empty unified diff referencing only the files listed
  in files_changed.
- Do not propose speculative changes in files outside the localization
  evidence unless the root cause strictly requires it; if you do, mention
  this in the explanation.
- Prefer minimal diffs. Do not reformat unrelated code.
- If the evidence is insufficient to propose a concrete fix, return a
  valid JSON object with confidence <= 0.2 and a diff field describing
  the investigation needed as a comment-only diff (no code changes)."""


FIX_PROPOSAL_USER_TEMPLATE = """\
Bug report:
  Title: {title}
  Description: {description}
  Steps to reproduce: {steps}
  Expected: {expected}
  Actual: {actual}
  Severity (reporter): {severity}

Localization evidence (from Stage 3):
{localization_evidence}

Candidate file contents:
{file_bundles}

Produce the fix proposal JSON object described in the system prompt."""


def _format_localization_evidence(analysis: dict[str, Any]) -> str:
    """Render a Stage-3 localization analysis into a compact evidence block.

    Reads the real Stage-3 schema:
        result = {
          "pass1": {"candidate_files": [{path, relevance, reason}, ...],
                    "confidence": ...},
          "pass2": {"localizations": [
                       {file, function, line_range, confidence, reason}, ...],
                    "root_cause_hypothesis": "..."},
          "repo_sha": "...",
        }

    Prefers pass2 entries (richer: function + line_range). Falls back to
    pass1 entries. Includes the root-cause hypothesis when present.
    """
    if not analysis:
        return "  (none available)"
    result = analysis.get("result")
    if not isinstance(result, dict):
        return "  (localization result missing or malformed)"

    lines: list[str] = []

    pass2 = result.get("pass2") if isinstance(result.get("pass2"), dict) else None
    pass1 = result.get("pass1") if isinstance(result.get("pass1"), dict) else None

    if pass2 and pass2.get("localizations"):
        for loc in pass2["localizations"]:
            if not isinstance(loc, dict):
                continue
            path = loc.get("file") or loc.get("path") or "?"
            function = loc.get("function") or ""
            line_range = loc.get("line_range") or []
            reason = loc.get("reason") or ""
            confidence = loc.get("confidence")
            conf_str = f"  confidence={confidence}" if confidence is not None else ""
            fn_str = f"  function={function}" if function else ""
            lines.append(
                f"  - {path}{fn_str}  line_range={line_range}{conf_str}  reason={reason}"
            )
        root_hyp = pass2.get("root_cause_hypothesis")
        if root_hyp:
            lines.append(f"  Root-cause hypothesis: {root_hyp}")
    elif pass1 and pass1.get("candidate_files"):
        for cand in pass1["candidate_files"]:
            if not isinstance(cand, dict):
                continue
            path = cand.get("path") or cand.get("file") or "?"
            relevance = cand.get("relevance")
            rel_str = f"  relevance={relevance}" if relevance is not None else ""
            reason = cand.get("reason") or ""
            lines.append(f"  - {path}{rel_str}  reason={reason}")

    return "\n".join(lines) if lines else "  (no candidate files returned)"


def _format_file_bundles(file_contents: dict[str, str]) -> str:
    """Render file bundles into labeled fenced blocks."""
    if not file_contents:
        return "  (no file contents provided)"
    chunks: list[str] = []
    for path, content in file_contents.items():
        chunks.append(f"### {path}\n```\n{content}\n```")
    return "\n\n".join(chunks)


def format_fix_proposal_prompt(
    report: dict[str, Any],
    analysis: dict[str, Any],
    file_contents: dict[str, str],
    *,
    enable_prompt_caching: bool = True,
) -> list[dict[str, Any]]:
    """Format a fix-proposal request.

    When `enable_prompt_caching` is True, the system prompt is wrapped in
    Anthropic's structured-content form with a `cache_control` marker so
    litellm can route it to prompt caching.
    """
    steps = report.get("steps_to_reproduce")
    if isinstance(steps, list):
        steps_text = "\n".join(f"    {i+1}. {s}" for i, s in enumerate(steps))
    else:
        steps_text = steps or "Not provided"

    user_content = FIX_PROPOSAL_USER_TEMPLATE.format(
        title=report.get("title", ""),
        description=report.get("description", ""),
        steps=steps_text,
        expected=report.get("expected_behavior") or "Not provided",
        actual=report.get("actual_behavior") or "Not provided",
        severity=report.get("severity", "medium"),
        localization_evidence=_format_localization_evidence(analysis),
        file_bundles=_format_file_bundles(file_contents),
    )

    if enable_prompt_caching:
        system_content: Any = [
            {
                "type": "text",
                "text": FIX_PROPOSAL_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ]
    else:
        system_content = FIX_PROPOSAL_SYSTEM_PROMPT

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]
