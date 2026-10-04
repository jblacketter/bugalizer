"""Action executor for sonicgrid admin actions (Phase 11 / B3).

Maps each action `kind` onto Bugalizer's own internals, in process (never its
own HTTP API), with the contract's semantics (sonicgrid
`documentation/BUGALIZER-TRIAGE-ENDPOINTS.md`, "Action semantics"):

- **refused**: the step did not run; it spent nothing and wrote nothing.
- **failed**: the step ran, and its side effects may be partial.

Authorization happens once, when an action moves to `intent`: the provider
and model of every LLM stage it will run are resolved and pinned, and the
cloud allowlist, the local-only rule for `analyze_local` and `no_auto_retry`
are decided from those pinned values (plan §4a, D-A). The ledger writes live
in `triage_sync`; this module computes outcomes.

Phase 12 (B4, `docs/phases/per-user-cloud-keys.md` §4 and §Rollout): with
`BUGALIZER_SONICGRID_USER_KEYS` on, a paid action's model comes only from its
`params.llm` pin and runs only on the requester's own key (`key_mode =
requester`); the allowlist is not consulted. With it off, an action carrying
`params.llm` is refused, so an own-key action never runs on the env key (I1).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from bugalizer import db
from bugalizer.api.reports import (
    check_cloud_analysis,
    check_local_analysis,
    check_status_transition,
)
from pydantic import SecretStr

from bugalizer.config import settings
from bugalizer.git_ops.pull_request import OpenPrError, open_pull_request
from bugalizer.llm.client import resolve_fix_llm, resolve_local_llm
from bugalizer.models import AnalysisMode, BugStatus, LLMOverride
from bugalizer.pipeline.fix_proposer import propose_fix
from bugalizer.pipeline.orchestrator import LocalOutcome, run_local_analysis

logger = logging.getLogger(__name__)

KINDS = ("analyze_local", "analyze_cloud", "fix_and_open_pr", "open_pr", "set_mode", "close", "reopen")
PAID_KINDS = frozenset({"analyze_cloud", "fix_and_open_pr"})
LLM_KINDS = frozenset({"analyze_local", "analyze_cloud", "fix_and_open_pr"})
LOCAL_PROVIDERS = frozenset({"ollama"})
MESSAGE_MAX = 2_000
MODEL_MAX = 200
REQUESTER = "requester"
REQUESTER_PROVIDER = "anthropic"
PR_STEP_MAX_ATTEMPTS = 3

# Open-pr codes raised before any remote write, or with the remote left
# untouched (contract "Open PR outcomes"): refused.
OPEN_PR_REFUSED = frozenset({
    "report_not_found", "proposal_not_found", "no_proposal", "wrong_status",
    "github_not_configured", "not_github", "not_cloned", "bad_default_branch",
    "bad_id", "pr_exists", "pr_unattributed", "diff_does_not_apply", "branch_exists",
})

ALLOWLIST_REFUSAL = (
    "Cloud analysis from sonicgrid is limited to allowlisted users "
    "(BUGALIZER_SONICGRID_CLOUD_USERS). Local analysis is available to every admin."
)
USER_KEYS_OFF_REFUSAL = "Per-user keys are not enabled on Bugalizer yet; nothing ran."
LEGACY_REFUSAL = "Requested before per-user keys; request again."
NO_PIN_REFUSAL = "Requested without a model pin; request again."
REOPENED = "Report reopened in Bugalizer"


def reopen_refusal(status: Optional[str]) -> str:
    return f"Only a closed report can be reopened; this one is {status or 'unknown'}"


def clip(text: Optional[str]) -> Optional[str]:
    return text[:MESSAGE_MAX] if isinstance(text, str) else None


def is_paid(provider: Optional[str]) -> bool:
    return (provider or "").strip().lower() not in LOCAL_PROVIDERS


def allowlisted(email: Optional[str]) -> bool:
    return bool(email) and email.strip().casefold() in settings.sonicgrid_cloud_user_set()


@dataclass
class Result:
    """An action outcome. `outcome` is done | failed | refused, or `fix_done`
    (compound checkpoint: run the PR step next)."""
    outcome: str
    message: Optional[str] = None
    steps: Optional[list[dict[str, Any]]] = None
    proposal_id: Optional[str] = None
    pr_url: Optional[str] = None


@dataclass
class Authorization:
    refusal: Optional[Result] = None
    pinned: dict[str, list[str]] = field(default_factory=dict)
    no_auto_retry: bool = False
    key_mode: Optional[str] = None


def compound_steps(fix: dict[str, Any], pr: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """The two `fix_and_open_pr` steps; after the first non-done step, the
    rest is `skipped` (contract "Fix and open PR")."""
    second = pr if pr is not None else {"step": "open_pr", "state": "skipped"}
    return [
        {k: v for k, v in fix.items() if v is not None},
        {k: v for k, v in second.items() if v is not None},
    ]


def step_result(kind: str, state: str, message: str) -> Result:
    """A one-step outcome; for `fix_and_open_pr` it is the fix step's state
    and the PR step is skipped."""
    if kind == "fix_and_open_pr":
        return Result(state, message, compound_steps(
            {"step": "fix", "state": state, "message": clip(message)}
        ))
    return Result(state, message)


def _refuse(kind: str, message: str) -> Result:
    return step_result(kind, "refused", message)


def requester_pin(params: Any) -> Optional[tuple[str, str]]:
    """The `params.llm` pin of an own-key paid action, or None when absent
    or invalid (fail closed: Anthropic only, a non-blank model)."""
    llm = params.get("llm") if isinstance(params, dict) else None
    if not isinstance(llm, dict):
        return None
    provider, model = llm.get("provider"), llm.get("model")
    if provider != REQUESTER_PROVIDER:
        return None
    if not isinstance(model, str) or not model.strip() or len(model) > MODEL_MAX:
        return None
    return provider, model


def _authorize_requester(project: dict[str, Any], report: dict[str, Any],
                         action: dict[str, Any], params: dict[str, Any]) -> Authorization:
    """Gate on: the pin comes only from `params.llm`; the key is fetched at
    dispatch (triage_sync). Every paid action is `no_auto_retry`."""
    kind = action["kind"]
    consents = action.get("consents") or {}
    if "llm" not in params:
        return Authorization(_refuse(kind, LEGACY_REFUSAL))
    pin = requester_pin(params)
    if pin is None:
        return Authorization(_refuse(kind, NO_PIN_REFUSAL))
    pinned = {"fix": list(pin)}
    if kind == "fix_and_open_pr":
        if consents.get("repoWrite") is not True:
            return Authorization(_refuse(kind, "The repo-write consent was not given."), pinned)
        if consents.get("cloudSpend") is not True:
            return Authorization(_refuse(kind, "The cloud-spend consent was not given."), pinned)
    refusal = check_cloud_analysis(report, project)
    if refusal is not None:
        return Authorization(_refuse(kind, refusal[1]), pinned)
    return Authorization(None, pinned, no_auto_retry=True, key_mode=REQUESTER)


def authorize(project: dict[str, Any], report: dict[str, Any], action: dict[str, Any]) -> Authorization:
    """Decide, once, whether this action may run and on what. The requester
    email is read here from the listed action and never stored (D-D)."""
    kind = action["kind"]
    email = (action.get("requestedBy") or {}).get("email")
    consents = action.get("consents") or {}

    if kind not in KINDS:
        return Authorization(_refuse(kind, f"Unknown action kind '{kind}'"))

    if kind == "analyze_local":
        triage = list(resolve_local_llm(project, stage="triage"))
        localize = list(resolve_local_llm(project, stage="localize"))
        pinned = {"triage": triage, "localization": localize}
        paid = [p for p, _ in (triage, localize) if is_paid(p)]
        if paid:
            return Authorization(_refuse(kind, (
                f"This project's local stages are configured for a non-local provider "
                f"({paid[0]}); Analyze (local) from sonicgrid runs only on local models."
            )), pinned)
        refusal = check_local_analysis(report)
        if refusal is not None:
            return Authorization(_refuse(kind, refusal[1]), pinned)
        return Authorization(None, pinned, no_auto_retry=False)

    if kind in PAID_KINDS:
        params = action.get("params") if isinstance(action.get("params"), dict) else {}
        if settings.sonicgrid_user_keys:
            return _authorize_requester(project, report, action, params)
        if "llm" in params:
            # An own-key action never runs on the legacy path (env key).
            return Authorization(_refuse(kind, USER_KEYS_OFF_REFUSAL))
        fix = list(resolve_fix_llm(project))
        pinned = {"fix": fix}
        paid = is_paid(fix[0])
        if kind == "fix_and_open_pr":
            if consents.get("repoWrite") is not True:
                return Authorization(_refuse(kind, "The repo-write consent was not given."), pinned)
            if paid and consents.get("cloudSpend") is not True:
                return Authorization(_refuse(kind, "The cloud-spend consent was not given."), pinned)
        # D-A: analyze_cloud always needs the allowlist; the fix step of
        # fix_and_open_pr only when it would run on a paid model.
        if (kind == "analyze_cloud" or paid) and not allowlisted(email):
            return Authorization(_refuse(kind, ALLOWLIST_REFUSAL), pinned)
        refusal = check_cloud_analysis(report, project)
        if refusal is not None:
            return Authorization(_refuse(kind, refusal[1]), pinned)
        # Recovery: analyze_cloud is never re-dispatched automatically, on any
        # provider; a fix step only when paid.
        return Authorization(None, pinned, no_auto_retry=(kind == "analyze_cloud" or paid))

    if kind == "open_pr":
        if consents.get("repoWrite") is not True:
            return Authorization(_refuse(kind, "The repo-write consent was not given."))
        return Authorization(None)

    if kind == "set_mode":
        mode = (action.get("params") or {}).get("mode")
        if mode not in {m.value for m in AnalysisMode}:
            return Authorization(_refuse(kind, "params.mode must be auto, local_only or hold"))
        return Authorization(None)

    if kind == "reopen":
        if report["status"] != BugStatus.CLOSED.value:
            return Authorization(_refuse(kind, reopen_refusal(report["status"])))
        return Authorization(None)

    # close
    refusal = check_status_transition(report, BugStatus.CLOSED)
    if refusal is not None:
        return Authorization(_refuse(kind, refusal[1]))
    return Authorization(None)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _pin(row: dict[str, Any], stage: str) -> Optional[tuple[str, str]]:
    value = (row.get("pinned_llm") or {}).get(stage)
    return (value[0], value[1]) if isinstance(value, list) and len(value) == 2 else None


def attribution_ref(row: dict[str, Any]) -> Optional[str]:
    user = row.get("requested_by_user_id")
    return f"sonicgrid:{user}"[:128] if user else None


def map_local(outcome: LocalOutcome) -> Result:
    if outcome is LocalOutcome.COMPLETED:
        return Result("done", "Local analysis finished: localization written")
    if outcome is LocalOutcome.FAILED:
        return Result("failed", "Local analysis ran and failed; see Bugalizer for the error")
    if outcome is LocalOutcome.TRIAGE_ONLY:
        return Result("failed", "Triage asked for clarification; no localization was produced")
    if outcome is LocalOutcome.ALREADY_FRESH:
        return Result("refused", "Localization is already current for this commit; nothing re-run")
    if outcome is LocalOutcome.NO_REPO:
        return Result("refused", "The project repo is not cloned on Bugalizer")
    return Result("refused", "Another analysis held this report; nothing ran")


def map_fix(kind: str, outcome: Any) -> Result:
    """A `propose_fix` outcome as an `analyze_cloud` result, or as the fix
    step of `fix_and_open_pr` (`fix_done` = go on to the PR step)."""
    if outcome.kind == "proposed":
        if kind == "fix_and_open_pr":
            return Result("fix_done", proposal_id=outcome.proposal_id)
        return Result("done", f"Fix proposal {outcome.proposal_id} made", proposal_id=outcome.proposal_id)
    if outcome.kind == "failed":
        state, message = "failed", f"The fix proposal failed: {outcome.error or 'error'}"
    elif outcome.kind == "already_proposed":
        state, message = "refused", (
            f"The latest localization already has fix proposal {outcome.proposal_id}; "
            "request Open PR for it, or re-run local analysis first"
        )
    elif outcome.kind == "deferred":
        state, message = "refused", f"The fix did not run: {outcome.error or 'precondition not met'}"
    else:
        state, message = "refused", "Another run held this report; nothing ran"
    if kind == "fix_and_open_pr":
        return Result(state, message, compound_steps(
            {"step": "fix", "state": state, "message": clip(message)}
        ))
    return Result(state, message)


async def run_llm(row: dict[str, Any], api_key: Optional[str] = None) -> Result:
    """Dispatch the LLM part of an action with its pinned configuration and
    tag every stage row with the action id. `api_key` is the requester's key
    for a `key_mode=requester` action (released once by sonicgrid); it lives
    only in this call."""
    kind, report_id, action_id = row["kind"], row["report_id"], row["action_id"]
    if row.get("key_mode") == REQUESTER:
        pin = _pin(row, "fix")
        if pin is None or pin[0] != REQUESTER_PROVIDER or not api_key:
            return step_result(kind, "failed", "invalid credential response")
        override = LLMOverride(
            provider=pin[0], model=pin[1], api_key=SecretStr(api_key),
            key_ref=attribution_ref(row),
        )
        outcome = await propose_fix(
            report_id, llm_override=override, trigger_ref=action_id,
            require_request_key=True,
        )
        return map_fix(kind, outcome)
    if kind == "analyze_local":
        outcome = await run_local_analysis(
            report_id,
            triage_llm=_pin(row, "triage"),
            localize_llm=_pin(row, "localization"),
            trigger_ref=action_id,
        )
        return map_local(outcome)
    pin = _pin(row, "fix")
    if settings.sonicgrid_user_keys and (pin is None or is_paid(pin[0])):
        # Gate on: an action authorized under the old rules never reaches a
        # paid model on the env key (I1).
        return _refuse(kind, LEGACY_REFUSAL)
    override = LLMOverride(provider=pin[0], model=pin[1]) if pin else None
    outcome = await propose_fix(
        report_id, llm_override=override,
        attribution_ref=attribution_ref(row), trigger_ref=action_id,
    )
    return map_fix(kind, outcome)


@dataclass
class StepResult:
    """One open-pr attempt. `state` None = not an outcome yet (in progress,
    or a transient failure under the attempt cap): stay claimed."""
    state: Optional[str]
    message: Optional[str] = None
    ref: Optional[str] = None
    attempts: int = 0


def _pushed_note(report_id: str, proposal_id: Optional[str]) -> str:
    proposals = db.fix_proposals_for_report(report_id)
    target = next((p for p in proposals if p["id"] == proposal_id), proposals[0] if proposals else None)
    if target and target.get("pushed_sha") and target.get("branch_name"):
        return f"branch `{target['branch_name']}` pushed; "
    return ""


async def pr_step(report_id: str, proposal_id: Optional[str], attempts: int) -> StepResult:
    """Run open-pr once (B2 is idempotent per report and resumes at the PR
    step on a branch it already pushed). One call per attempt."""
    try:
        _status, body = await open_pull_request(report_id, proposal_id)
    except OpenPrError as exc:
        if exc.code in OPEN_PR_REFUSED:
            message = exc.message
            pr_url = exc.extra.get("pr_url")
            if pr_url:
                message = f"{message} ({pr_url})"
            return StepResult("refused", f"{exc.code}: {message}", attempts=attempts)
        if exc.code == "in_progress":
            return StepResult(None, attempts=attempts)
        detail = exc.code
    except Exception as exc:  # a lost response, a timeout, anything unexpected
        detail = type(exc).__name__
    else:
        verb = "opened" if body.get("created", True) else "already open"
        return StepResult(
            "done", f"PR #{body['pr_number']} {verb} on {body['branch']}",
            ref=body["pr_url"], attempts=attempts,
        )
    attempts += 1
    if attempts < PR_STEP_MAX_ATTEMPTS:
        return StepResult(None, attempts=attempts)
    return StepResult(
        "failed",
        f"{_pushed_note(report_id, proposal_id)}PR creation failed after "
        f"{attempts} attempts ({detail}); a later Open PR request resumes from here",
        attempts=attempts,
    )


def run_inline(row: dict[str, Any]) -> Result:
    """`set_mode` and `close`: immediate, no LLM."""
    report = db.report_get(row["report_id"])
    if report is None:
        return Result("refused", "Bug report not found in Bugalizer")
    if row["kind"] == "set_mode":
        mode = (row.get("params") or {}).get("mode")
        try:
            db.report_update_fields(row["report_id"], analysis_mode=mode)
        except Exception as exc:
            return Result("failed", f"Could not set the mode ({type(exc).__name__})")
        return Result("done", f"Analysis mode set to {mode}")
    if report["status"] == BugStatus.CLOSED.value:
        # A re-run after a crash between the close and the ledger write.
        return Result("done", "Report closed in Bugalizer")
    refusal = check_status_transition(report, BugStatus.CLOSED)
    if refusal is not None:
        return Result("refused", refusal[1])
    try:
        updated = db.report_update_status(
            row["report_id"], BugStatus.CLOSED.value, expected_status=report["status"]
        )
    except Exception as exc:
        return Result("failed", f"Could not close the report ({type(exc).__name__})")
    if updated is None:
        return Result("refused", "Report status changed during the update; request again")
    return Result("done", "Report closed in Bugalizer")


# ---------------------------------------------------------------------------
# Recovery evidence (rows tagged with this action's id only)
# ---------------------------------------------------------------------------

def _finished_rows(action_id: str) -> list[dict[str, Any]]:
    """This action's analysis rows, minus attempts cut off by a restart.

    `release_orphaned_claims` fails a dead process's `running` rows with
    `interrupted: true` so retry caps count them. The run did not finish, so
    the row is no outcome: recovery re-runs free work and reports paid work
    as unconfirmed, as if the row were still `running`.
    """
    return [
        a for a in db.analyses_by_trigger(action_id)
        if not (isinstance(a.get("result"), dict) and a["result"].get("interrupted"))
    ]


def evidence(row: dict[str, Any]) -> Optional[Result]:
    """The finished outcome this action's own stage rows show, or None when
    they show nothing finished (no rows, a row still `running`, or only
    interrupted attempts)."""
    action_id, kind = row["action_id"], row["kind"]
    if kind == "analyze_local":
        rows = _finished_rows(action_id)
        loc = [a for a in rows if a.get("phase") == "localization"]
        if any(a.get("status") == "completed" for a in loc):
            return Result("done", "Local analysis finished: localization written")
        if any(a.get("status") == "failed" for a in rows):
            return Result("failed", "Local analysis ran and failed; see Bugalizer for the error")
        return None
    if kind in ("analyze_cloud", "fix_and_open_pr"):
        proposals = db.fix_proposals_by_trigger(action_id)
        if proposals:
            pid = proposals[0]["id"]
            if kind == "fix_and_open_pr":
                return Result("fix_done", proposal_id=pid)
            return Result("done", f"Fix proposal {pid} made", proposal_id=pid)
        failed = [a for a in _finished_rows(action_id)
                  if a.get("phase") == "fix" and a.get("status") == "failed"]
        if failed:
            message = "The fix proposal failed; see Bugalizer for the error"
            if kind == "fix_and_open_pr":
                return Result("failed", message, compound_steps(
                    {"step": "fix", "state": "failed", "message": message}
                ))
            return Result("failed", message)
    return None


def stage_claim_held(report_id: str) -> bool:
    """True while the report sits in a pipeline stage's transient claim."""
    report = db.report_get(report_id)
    return bool(report) and report["status"] in (
        BugStatus.ANALYZING.value, BugStatus.FIX_PROPOSING.value
    )


def unconfirmed_failure(kind: str) -> Result:
    message = ("dispatch could not be confirmed; not retried automatically to avoid "
               "a second cloud charge; request again")
    if kind == "fix_and_open_pr":
        return Result("failed", message, compound_steps(
            {"step": "fix", "state": "failed", "message": message}
        ))
    return Result("failed", message)
