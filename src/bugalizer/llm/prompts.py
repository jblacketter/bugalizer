"""Prompt templates for pipeline stages."""

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
