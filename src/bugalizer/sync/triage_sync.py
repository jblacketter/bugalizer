"""Triage sync with sonicgrid (Phase 11 / B3 sonicgrid-triage-sync).

Sonicgrid runs on Vercel and cannot reach BOWIE, so every exchange is
Bugalizer calling sonicgrid (contract: sonicgrid
`documentation/BUGALIZER-TRIAGE-ENDPOINTS.md`). Each tick, per project with
`ingest_config.triage_credential_env` set, under one per-project lock:

1. **Terminal drain**: POST every finished, unacknowledged ledger outcome,
   whether or not the action is still listed (a committed terminal POST whose
   response was lost has left the open-only listing).
2. **Action walk**: `GET /actions` from no cursor to `next_cursor: null`
   (cursor never stored). New actions are reserved in the ledger and claimed;
   claimed ones follow the contract's recovery table (`_recover`).
3. A second drain for what finished inline this tick.
4. **Results push**: `PUT /results/{bugId}` for sonicgrid-sourced reports
   whose payload fingerprint changed (`sync/results.py`).

The triage token is resolved by name from the environment at request time
and sent only in the Authorization header; it is never stored, logged or
returned, and redirects are not followed. Requester emails are read from the
listing for the allowlist check and never stored or logged (D-D).

Phase 12 (B4): before the sonicgrid exchange, each tick also reads the fate
of recorded fix PRs on GitHub (`_check_prs`; merged completes the report,
closed unmerged returns it to `triaged`). A `key_mode=requester` action
fetches its requester's key once, from sonicgrid's credential endpoint,
after it is durably `dispatched` and before the model call (`_fetch_key`);
the key is never stored, logged or retried.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import weakref
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from bugalizer import db
from bugalizer.config import resolve_env_credential, settings
from bugalizer.git_ops.pull_request import PrReadError, read_pr_state
from bugalizer.models import triage_base_url
from bugalizer.sync import actions as ex
from bugalizer.sync.results import build_payload, canonical, fingerprint

logger = logging.getLogger(__name__)

# Fixed error vocabulary: state rows and logs carry only these.
UNAUTHORIZED = "unauthorized"
SOURCE_NOT_CONFIGURED = "source_not_configured"
UPSTREAM_ERROR = "upstream_error"
NETWORK_ERROR = "network_error"
TIMEOUT = "timeout"
MALFORMED_RESPONSE = "malformed_response"
UNEXPECTED_STATUS = "unexpected_status"
CREDENTIAL_MISSING = "credential_missing"
NOT_CONFIGURED = "not_configured"
DUPLICATE_SOURCE = "duplicate_triage_source"
INTERNAL_ERROR = "internal_error"

_BACKOFF_ERRORS = frozenset({UNAUTHORIZED, SOURCE_NOT_CONFIGURED, NETWORK_ERROR, TIMEOUT})
MAX_BACKOFF_TICKS = 32
PAGE_LIMIT = 100
MAX_PAGES = 100
PR_CHECK_SECONDS = 300            # at most one GitHub read per PR per 5 minutes
CREDENTIAL_FETCH_FAILED = "could not fetch your API key; request again"

# Test seam: tests point the sync at a fake sonicgrid via httpx.MockTransport.
http_transport: Optional[httpx.AsyncBaseTransport] = None

_task: Optional[asyncio.Task] = None
_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)
_skip: dict[str, int] = {}


@dataclass
class _Hold:
    """An executor slot: one action per report, and LLM actions counted
    against `triage_max_concurrent`. Released only when the work has
    actually stopped (a timed-out task keeps its hold until it exits)."""
    report_id: str
    llm: bool
    task: Optional[asyncio.Task] = None


_holds: dict[str, _Hold] = {}


class TickInProgress(Exception):
    """A manual run found this project's tick already running."""


class _Abort(Exception):
    """A route answered in a way that ends this project's tick."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class TickOutcome:
    actions_seen: int = 0
    actions_started: int = 0
    terminals_posted: int = 0
    results_pushed: int = 0
    unresolved: int = 0
    error: Optional[str] = None
    consecutive_failures: int = 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _epoch_us() -> int:
    return int(time.time() * 1_000_000)


def _project_lock(project_id: str) -> asyncio.Lock:
    per_loop = _locks.setdefault(asyncio.get_running_loop(), {})
    return per_loop.setdefault(project_id, asyncio.Lock())


# ---------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------

def _task_running(action_id: str) -> bool:
    hold = _holds.get(action_id)
    return bool(hold and hold.task and not hold.task.done())


def _try_hold(action_id: str, report_id: str, llm: bool) -> bool:
    """Take a slot, synchronously (no await between check and take, so it is
    atomic for every coroutine on this loop)."""
    if action_id in _holds:
        return True
    for other_id, hold in _holds.items():
        if hold.report_id == report_id:
            return False
    if llm:
        running = sum(1 for h in _holds.values() if h.llm)
        if running >= max(1, settings.triage_max_concurrent):
            return False
    _holds[action_id] = _Hold(report_id, llm)
    return True


def _release(action_id: str) -> None:
    hold = _holds.get(action_id)
    if hold is not None and (hold.task is None or hold.task.done()
                             or hold.task is asyncio.current_task()):
        _holds.pop(action_id, None)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class _Api:
    def __init__(self, client: httpx.AsyncClient, base: str, token: str) -> None:
        self.client = client
        self.base = base
        self.token = token

    async def _request(self, method: str, path: str, *,
                       extra_headers: Optional[dict[str, str]] = None,
                       **kwargs: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token}", **(extra_headers or {})}
        try:
            resp = await self.client.request(method, self.base + path, headers=headers, **kwargs)
        except httpx.TimeoutException:
            raise _Abort(TIMEOUT) from None
        except httpx.HTTPError:
            raise _Abort(NETWORK_ERROR) from None
        if resp.status_code == 401:
            raise _Abort(UNAUTHORIZED)
        if resp.status_code == 503:
            raise _Abort(SOURCE_NOT_CONFIGURED)
        if 500 <= resp.status_code < 600:
            raise _Abort(UPSTREAM_ERROR)
        return resp

    @staticmethod
    def _json(resp: httpx.Response) -> dict[str, Any]:
        try:
            body = resp.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    async def list_actions(self) -> list[dict[str, Any]]:
        """Every open action, walking from no cursor to `next_cursor: null`."""
        out: list[dict[str, Any]] = []
        cursor: Optional[str] = None
        for _ in range(MAX_PAGES):
            params = {"limit": str(PAGE_LIMIT)}
            if cursor is not None:
                params["cursor"] = cursor
            resp = await self._request("GET", "/actions", params=params)
            if resp.status_code != 200:
                raise _Abort(f"{UNEXPECTED_STATUS}:{resp.status_code}")
            body = self._json(resp)
            items = body.get("actions")
            if not isinstance(items, list):
                raise _Abort(MALFORMED_RESPONSE)
            out += [a for a in items if _well_formed(a)]
            cursor = body.get("next_cursor")
            if not isinstance(cursor, str) or not cursor:
                break
        return out

    async def advance(self, action_id: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        resp = await self._request("POST", f"/actions/{action_id}", json=body)
        return resp.status_code, self._json(resp)

    async def put_result(self, bug_id: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        resp = await self._request(
            "PUT", f"/results/{bug_id}", content=canonical(payload).encode("utf-8"),
            extra_headers={"Content-Type": "application/json"},
        )
        return resp.status_code, self._json(resp)


def _well_formed(action: Any) -> bool:
    return (
        isinstance(action, dict)
        and isinstance(action.get("id"), str) and action["id"]
        and isinstance(action.get("bugId"), str) and action["bugId"]
        and isinstance(action.get("kind"), str)
        and action.get("state") in ("pending", "claimed")
    )


# ---------------------------------------------------------------------------
# Ledger helpers
# ---------------------------------------------------------------------------

def _terminal_body(row: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"state": row["outcome"]}
    if row.get("message"):
        body["message"] = ex.clip(row["message"])
    if row["kind"] == "fix_and_open_pr":
        body["steps"] = row.get("steps") or ex.compound_steps(
            {"step": "fix", "state": row["outcome"]}
        )
    return body


def _finish(action_id: str, result: ex.Result, *, expected_phase: Optional[str] = None) -> bool:
    finished = db.triage_action_finish(
        action_id, result.outcome, ex.clip(result.message), result.steps,
        expected_phase=expected_phase,
    )
    if finished and result.pr_url:
        db.triage_action_update(action_id, pr_url=result.pr_url)
    if not finished:
        # A late completion (the row was finished by the time bound, or moved
        # on): kept for the operator only; the posted outcome stands.
        db.triage_action_update(action_id, late_outcome=result.outcome)
    return finished


def _timed_out(row: dict[str, Any]) -> bool:
    started = row.get("intent_at")
    if not started:
        return False
    try:
        started_dt = datetime.fromisoformat(started)
    except ValueError:
        return True
    bound = timedelta(minutes=settings.triage_action_timeout_minutes)
    return datetime.now(timezone.utc) - started_dt >= bound


def _timeout_result(row: dict[str, Any]) -> ex.Result:
    minutes = settings.triage_action_timeout_minutes
    message = f"timed out after {minutes:g} min"
    if row["kind"] == "fix_and_open_pr":
        steps = row.get("steps") or []
        if row["phase"] == "fix_done" and steps:
            return ex.Result("failed", message, ex.compound_steps(
                steps[0], {"step": "open_pr", "state": "failed", "message": message}
            ))
        return ex.Result("failed", message, ex.compound_steps(
            {"step": "fix", "state": "failed", "message": message}
        ))
    return ex.Result("failed", message)


def _expire_if_overdue(action_id: str) -> bool:
    """Finish the action `failed` ("timed out") if its bound has passed.
    True when the action is finished, by this call or before it. Local only:
    it never depends on sonicgrid answering; the drain posts it later."""
    row = db.triage_action_get(action_id)
    if row is None or row["phase"] == "finished":
        return True
    if _timed_out(row):
        _finish(action_id, _timeout_result(row))
        return True
    return False


def expire_overdue(project_id: Optional[str] = None) -> int:
    """Apply the time bound to every open ledger row, whatever the state of
    the action listing (an outage or backoff must not stretch the bound)."""
    expired = 0
    for row in db.triage_actions_open(project_id):
        if _timed_out(row) and _finish(row["action_id"], _timeout_result(row)):
            expired += 1
    return expired


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _run_inline(row: dict[str, Any]) -> None:
    """An inline kind from `dispatched`. `reopen` writes the report change
    and its ledger outcome in one transaction (plan §3), so a committed
    reopen is never replayed; the rest finish through `_finish`."""
    action_id = row["action_id"]
    if row["kind"] != "reopen":
        _finish(action_id, ex.run_inline(row), expected_phase="dispatched")
        return
    applied = db.reopen_for_action(row["report_id"], action_id, ex.REOPENED)
    if applied == "done" or applied == "ledger_moved":
        return  # finished by this call, or the row moved on (nothing changed)
    report = db.report_get(row["report_id"])
    message = (ex.reopen_refusal(report["status"]) if report
               else "Bug report not found in Bugalizer")
    _finish(action_id, ex.Result("refused", message), expected_phase="dispatched")


def _credential_code(body: dict[str, Any]) -> str:
    code = body.get("error") or body.get("code")
    if isinstance(code, str) and code and len(code) <= 40 and all(c.islower() or c == "_" for c in code):
        return code
    return "unknown"


def _credential_outcome(row: dict[str, Any], resp: httpx.Response) -> "str | ex.Result":
    """The plan's §4 table: the key on a valid 200, else the action's result.
    Never logs or echoes the body."""
    kind = row["kind"]
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        body = None
    status = resp.status_code
    if status == 200:
        pin = (row.get("pinned_llm") or {}).get("fix")
        pinned_provider = pin[0] if isinstance(pin, list) and pin else None
        key = (body or {}).get("apiKey")
        provider = (body or {}).get("provider")
        if (provider == ex.REQUESTER_PROVIDER and provider == pinned_provider
                and isinstance(key, str) and key):
            return key
        return ex.step_result(kind, "failed", "invalid credential response")
    code = _credential_code(body or {})
    if status == 409 and code == "already_released":
        return ex.step_result(kind, "failed", "credential already used; request again")
    if status == 409 and code == "no_key":
        return ex.step_result(kind, "refused", (
            "No Claude API key is saved for the requester; add it in AI settings "
            "and request again"
        ))
    if status == 403:
        return ex.step_result(kind, "refused", "The requester is no longer a sonicgrid admin")
    if status in (404, 409):
        return ex.step_result(kind, "refused", (
            f"Sonicgrid did not release a key for this action ({code if status == 409 else 'not_found'})"
        ))
    return ex.step_result(kind, "failed", CREDENTIAL_FETCH_FAILED)


async def _fetch_key(row: dict[str, Any]) -> "str | ex.Result":
    """One credential request for a claimed paid action (no body; the
    triage token in the header). A lost response is not retried: the key
    may have been released, and I3 allows one release per action."""
    kind = row["kind"]
    project = db.project_get(row["project_id"]) or {}
    config = project.get("ingest_config")
    env_name = config.get("triage_credential_env") if isinstance(config, dict) else None
    base = triage_base_url(config) if isinstance(config, dict) else None
    token = (resolve_env_credential(env_name) or "") if env_name else ""
    if not base or not token:
        return ex.step_result(kind, "failed", CREDENTIAL_FETCH_FAILED)
    try:
        async with httpx.AsyncClient(
            timeout=settings.ingest_timeout_seconds, follow_redirects=False,
            transport=http_transport,
        ) as client:
            resp = await client.post(
                f"{base}/actions/{row['action_id']}/credential",
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        logger.warning("triage action=%s credential fetch failed (%s)",
                       row["action_id"], type(exc).__name__)
        return ex.step_result(kind, "failed", CREDENTIAL_FETCH_FAILED)
    finally:
        token = ""
    outcome = _credential_outcome(row, resp)
    if not isinstance(outcome, str):
        logger.warning("triage action=%s credential answered %d",
                       row["action_id"], resp.status_code)
    return outcome


async def _run_pr(action_id: str) -> None:
    """The open-pr step: an `open_pr` action, or `fix_and_open_pr` after its
    `fix_done` checkpoint (with that checkpoint's proposal id only)."""
    row = db.triage_action_get(action_id)
    if row is None:
        return
    compound = row["kind"] == "fix_and_open_pr"
    expected = "fix_done" if compound else "dispatched"
    if row["phase"] != expected:
        return  # finished (e.g. by the time bound): never start the PR step
    if _expire_if_overdue(action_id):
        return  # the bound passed: no further side effect
    step = await ex.pr_step(row["report_id"], row.get("fix_proposal_id") if compound else None,
                            int(row.get("attempts") or 0))
    if _expire_if_overdue(action_id):
        # The bound passed during the call: the timeout outcome stands (with
        # any completed fix step), the late answer is kept for the operator,
        # and no further attempt is scheduled.
        db.triage_action_update(action_id, late_outcome=step.state or "pr_pending")
        return
    if step.state is None:
        db.triage_action_update(action_id, attempts=step.attempts, expected_phase=expected)
        return
    if compound:
        fix_step = (row.get("steps") or [{"step": "fix", "state": "done"}])[0]
        result = ex.Result(step.state, step.message, ex.compound_steps(fix_step, {
            "step": "open_pr", "state": step.state, "ref": step.ref,
            "message": ex.clip(step.message),
        }), pr_url=step.ref)
        if step.state == "done":
            result.message = f"Fix proposed and {step.message}"
        elif step.state == "refused":
            result.message = f"Fix proposed; the PR was refused: {step.message}"
        else:
            result.message = f"Fix proposed; the PR step failed: {step.message}"
    else:
        message = f"{step.message}: {step.ref}" if step.ref else step.message
        result = ex.Result(step.state, message, pr_url=step.ref)
    _finish(action_id, result, expected_phase=expected)


def _checkpoint_fix(action_id: str, proposal_id: str, from_phase: str) -> bool:
    """`fix_and_open_pr`: persist `fix: done` and its proposal id before the
    PR step, in one write. False when the row has moved on (timed out)."""
    return db.triage_action_update(
        action_id, expected_phase=from_phase, phase="fix_done", fix_proposal_id=proposal_id,
        steps=[{"step": "fix", "state": "done", "ref": proposal_id,
                "message": "Proposal made"}],
    )


async def _pr_task(action_id: str) -> None:
    """The PR step as a tracked task holding its slot until the call exits,
    so the tick (and its expiry checks) never waits on GitHub."""
    try:
        await _run_pr(action_id)
    finally:
        _release(action_id)


def _spawn_pr(action_id: str) -> None:
    """Start the PR step for an action whose slot the caller holds."""
    _holds[action_id].task = asyncio.create_task(
        _pr_task(action_id), name=f"bugalizer-triage-pr-{action_id}"
    )


async def _run_task(action_id: str) -> None:
    """An LLM action, run as a tracked task holding its slot until it exits."""
    try:
        row = db.triage_action_get(action_id)
        if row is None:
            return
        api_key: Optional[str] = None
        try:
            fetched = await _fetch_key(row) if row.get("key_mode") == ex.REQUESTER else None
            if isinstance(fetched, ex.Result):
                result = fetched  # no model call
            else:
                api_key = fetched
                result = await ex.run_llm(row, api_key)
        except Exception as exc:
            logger.error("triage action=%s failed (%s)", action_id, type(exc).__name__)
            message = f"The analysis failed unexpectedly ({type(exc).__name__})"
            result = ex.Result("failed", message, ex.compound_steps(
                {"step": "fix", "state": "failed", "message": message}
            ) if row["kind"] == "fix_and_open_pr" else None)
        finally:
            api_key = None
        if _expire_if_overdue(action_id):
            # Finished late: the posted outcome stands, the PR step never runs.
            db.triage_action_update(action_id, late_outcome=result.outcome)
            return
        if result.outcome == "fix_done":
            if not _checkpoint_fix(action_id, result.proposal_id or "", "dispatched"):
                db.triage_action_update(action_id, late_outcome="fix_done")
                return
            await _run_pr(action_id)
        else:
            _finish(action_id, result, expected_phase="dispatched")
    finally:
        _release(action_id)


async def _dispatch(row: dict[str, Any], outcome: TickOutcome, *, from_phase: str) -> None:
    """Start an authorized action. The caller holds its slot."""
    action_id, kind = row["action_id"], row["kind"]
    if not db.triage_action_update(
        action_id, expected_phase=from_phase, phase="dispatched", dispatched_at=_now_iso()
    ):
        _release(action_id)
        return
    outcome.actions_started += 1
    if kind in ex.LLM_KINDS:
        _holds[action_id].task = asyncio.create_task(
            _run_task(action_id), name=f"bugalizer-triage-{action_id}"
        )
        return
    if kind == "open_pr":
        _spawn_pr(action_id)
        return
    try:
        _run_inline(row)
    finally:
        _release(action_id)


async def _claim_and_start(api: _Api, project: dict[str, Any], action: dict[str, Any],
                           outcome: TickOutcome) -> None:
    """Reserved → claimed → intent (authorized and pinned) → dispatched. The
    caller holds the slot; it is released here unless a task took it over."""
    action_id = action["id"]
    try:
        status, _body = await api.advance(action_id, {"state": "claimed"})
        if status in (404, 409):
            # 409: someone else's claim, or terminal — don't run. 404: gone.
            db.triage_action_delete_reserved(action_id)
            return
        if status != 200:
            return  # stays reserved; the next tick re-POSTs `claimed`
        row = db.triage_action_get(action_id)
        report = db.report_get(row["report_id"]) if row else None
        if row is None or report is None:
            return
        auth = ex.authorize(project, report, action)
        if auth.refusal is not None:
            # Finish straight from `reserved`, in one write. A refused action
            # never gets an `intent` row, so recovery (which dispatches intent
            # rows without re-authorizing) can never run it; a crash before
            # this write leaves it reserved, and the next tick re-authorizes.
            _finish(action_id, auth.refusal, expected_phase="reserved")
            return
        if not db.triage_action_update(
            action_id, expected_phase="reserved", phase="intent", intent_at=_now_iso(),
            pinned_llm=auth.pinned, no_auto_retry=int(auth.no_auto_retry),
            key_mode=auth.key_mode,
        ):
            return
        await _dispatch(db.triage_action_get(action_id), outcome, from_phase="intent")
    finally:
        if not _task_running(action_id):
            _release(action_id)


async def _apply_evidence(row: dict[str, Any], found: ex.Result, outcome: TickOutcome) -> None:
    action_id = row["action_id"]
    if found.outcome == "fix_done":
        if _checkpoint_fix(action_id, found.proposal_id or "", row["phase"]):
            if _try_hold(action_id, row["report_id"], llm=False):
                _spawn_pr(action_id)
        return
    _finish(action_id, found, expected_phase=row["phase"])


async def _recover(row: dict[str, Any], outcome: TickOutcome) -> None:
    """The contract's recovery table, for a claimed action with a ledger row
    and no task running in this process."""
    action_id, kind, phase = row["action_id"], row["kind"], row["phase"]
    if _timed_out(row):
        _finish(action_id, _timeout_result(row))
        return
    llm = kind in ex.LLM_KINDS

    if phase == "fix_done":
        if _try_hold(action_id, row["report_id"], llm=False):
            _spawn_pr(action_id)
        return

    if phase == "dispatched" and not llm:
        # Inline kinds crashed mid-call, or open-pr said in_progress: all
        # spend nothing and are idempotent, so run them again.
        if _try_hold(action_id, row["report_id"], llm=False):
            if kind == "open_pr":
                _spawn_pr(action_id)
                return
            try:
                _run_inline(row)
            finally:
                _release(action_id)
        return

    if phase in ("intent", "dispatched"):
        found = ex.evidence(row)
        if found is not None:
            await _apply_evidence(row, found, outcome)
            return
        if phase == "dispatched" and ex.stage_claim_held(row["report_id"]):
            return  # still inside a stage claim: wait, up to the time bound
        if (phase == "intent" and settings.sonicgrid_user_keys and kind in ex.PAID_KINDS
                and row.get("key_mode") != ex.REQUESTER):
            # Drain rule: authorized under the old rules, never dispatched.
            _finish(action_id, ex.step_result(kind, "refused", ex.LEGACY_REFUSAL),
                    expected_phase=phase)
            return
        if row.get("no_auto_retry"):
            _finish(action_id, ex.unconfirmed_failure(kind), expected_phase=phase)
            return
        if not _try_hold(action_id, row["report_id"], llm):
            return
        try:
            await _dispatch(row, outcome, from_phase=phase)
        finally:
            if not _task_running(action_id):
                _release(action_id)


async def _handle(api: _Api, project: dict[str, Any], action: dict[str, Any],
                  outcome: TickOutcome) -> None:
    action_id = action["id"]
    row = db.triage_action_get(action_id)

    if row is None:
        report = db.report_by_external_id(project["id"], action["bugId"])
        if report is None:
            outcome.unresolved += 1  # not imported yet: leave it pending
            return
        llm = action["kind"] in ex.LLM_KINDS
        # Check, insert and hold with no await in between: atomic on this loop.
        if not _try_hold(action_id, report["id"], llm):
            return
        user = (action.get("requestedBy") or {}).get("userId")
        if not db.triage_action_reserve(
            action_id, project["id"], report["id"], action["kind"],
            action.get("params") if isinstance(action.get("params"), dict) else {},
            user if isinstance(user, str) else None,
        ):
            _release(action_id)
            return
        await _claim_and_start(api, project, action, outcome)
        return

    if row["phase"] == "finished" or _task_running(action_id):
        if _task_running(action_id) and row["phase"] != "finished" and _timed_out(row):
            _finish(action_id, _timeout_result(row))
        return
    if row["phase"] == "reserved":
        if _try_hold(action_id, row["report_id"], row["kind"] in ex.LLM_KINDS):
            await _claim_and_start(api, project, action, outcome)
        return
    await _recover(row, outcome)


async def _drain(api: _Api, project_id: str, outcome: TickOutcome) -> None:
    for row in db.triage_actions_unacked(project_id):
        status, body = await api.advance(row["action_id"], _terminal_body(row))
        current = body.get("currentState") or (body.get("action") or {}).get("state")
        if status == 200:
            db.triage_action_update(row["action_id"], terminal_acked=1)
            outcome.terminals_posted += 1
        elif status == 409 and current == row["outcome"]:
            db.triage_action_update(row["action_id"], terminal_acked=1)
        elif status in (400, 404, 409):
            # A contract breach: surfaced on the status endpoint, not retried forever.
            db.triage_action_update(
                row["action_id"], terminal_acked=1,
                terminal_error=f"http_{status}" + (f":{current}" if current else ""),
            )
            logger.warning("triage action=%s terminal POST answered %s", row["action_id"], status)


# ---------------------------------------------------------------------------
# Results push
# ---------------------------------------------------------------------------

async def _push(api: _Api, project: dict[str, Any], outcome: TickOutcome) -> None:
    todo: list[tuple[str, dict[str, Any], str, Optional[dict[str, Any]], Optional[dict[str, Any]]]] = []
    for report in db.sync_reports(project["id"]):
        core = build_payload(
            report, db.analyses_for_report(report["id"]), db.fix_proposals_for_report(report["id"]),
            project,
        )
        fp = fingerprint(core)
        stored = db.triage_result_get(report["id"])
        if stored and stored.get("fingerprint") == fp:
            if fp in (stored.get("acked_fingerprint"), stored.get("failed_fingerprint")):
                continue
            todo.append((core["bugalizerUpdatedAt"], report, fp, None, stored))  # resend as stored
        else:
            todo.append((core["bugalizerUpdatedAt"], report, fp, core, stored))
    todo.sort(key=lambda item: item[0])
    for _stamp, report, fp, core, stored in todo[: max(1, settings.triage_push_per_tick)]:
        if core is not None:
            last = int(stored["revision"]) if stored else -1
            revision = max(last + 1, _epoch_us())
            payload = {"revision": revision, **core}
            db.triage_result_stage(report["id"], project["id"], fp, revision, canonical(payload))
        else:
            payload = json.loads(stored["payload"])  # type: ignore[index]
            revision = int(payload["revision"])
        status, body = await api.put_result(report["external_id"], payload)
        now = _now_iso()
        if status == 200:
            updates: dict[str, Any] = {"acked_fingerprint": fp, "last_status": 200,
                                       "last_error": None, "pushed_at": now}
            stored_rev = body.get("storedRevision")
            if body.get("applied") is False and isinstance(stored_rev, int) and stored_rev >= revision:
                updates["revision"] = stored_rev  # the next change is issued above it
            db.triage_result_update(report["id"], **updates)
            outcome.results_pushed += 1
        elif status in (400, 404, 413):
            db.triage_result_update(report["id"], failed_fingerprint=fp, last_status=status,
                                    last_error=f"http_{status}", pushed_at=now)
        else:
            db.triage_result_update(report["id"], last_status=status,
                                    last_error=f"{UNEXPECTED_STATUS}:{status}", pushed_at=now)


# ---------------------------------------------------------------------------
# PR state (plan §2)
# ---------------------------------------------------------------------------

async def _check_prs(project: dict[str, Any]) -> None:
    """Read each unsettled recorded PR of a `fix_committed` report, at most
    once per PR_CHECK_SECONDS. A failed read changes nothing but the check
    time and error code; a rate limit ends this tick's reads."""
    if settings.github_token_value() is None:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=PR_CHECK_SECONDS)).isoformat()
    for proposal in db.prs_to_check(project["id"], cutoff):
        number = int(proposal["pr_number"])
        try:
            state = await read_pr_state(project, number)
        except PrReadError as err:
            db.pr_check_record(proposal["id"], error=err.code)
            logger.warning("triage project=%s PR #%d read failed (%s)",
                           project["id"], number, err.code)
            if err.code == "rate_limited":
                return
            continue
        if state == "open":
            db.pr_check_record(proposal["id"], state="open")
            continue
        moved = db.pr_settle(proposal["id"], proposal["bug_report_id"], number, state)
        logger.info("triage project=%s PR #%d %s (report %s)", project["id"], number, state,
                    "moved" if moved else "status left alone")


async def check_prs_only(project_id: str) -> None:
    """The PR step alone, for a project whose sonicgrid exchange is in
    backoff: GitHub is not sonicgrid, so a sonicgrid outage must not delay
    merge detection. Same project lock as a tick; if a tick holds it, that
    tick runs the step itself. The per-PR interval is durable, so this never
    reads a PR more often than an ordinary tick would."""
    lock = _project_lock(project_id)
    if lock.locked():
        return
    async with lock:
        project = db.project_get(project_id)
        if project:
            await _check_prs(project)


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------

def _duplicate_source(project: dict[str, Any], base: str) -> bool:
    return any(
        other["id"] != project["id"] and triage_base_url(other.get("ingest_config")) == base
        for other in db.projects_with_triage_sync()
    )


async def sync_project(project_id: str, *, manual: bool = False) -> TickOutcome:
    """One tick for one project. A manual run that finds the tick running
    raises TickInProgress and does nothing."""
    lock = _project_lock(project_id)
    if manual and lock.locked():
        raise TickInProgress
    async with lock:
        outcome = TickOutcome()
        project = db.project_get(project_id)
        config = (project or {}).get("ingest_config")
        env_name = config.get("triage_credential_env") if isinstance(config, dict) else None
        base = triage_base_url(config) if isinstance(config, dict) else None
        if not project or not project.get("ingest_source") or not env_name or not base:
            outcome.error = NOT_CONFIGURED
            return outcome
        token = ""
        expire_overdue(project_id)  # before any HTTP: the bound never waits on sonicgrid
        try:
            # GitHub, not sonicgrid: a sonicgrid outage never delays a merge.
            await _check_prs(project)
        except Exception as exc:
            logger.error("triage project=%s PR check failed (%s)", project_id, type(exc).__name__)
        try:
            if _duplicate_source(project, base):
                outcome.error = DUPLICATE_SOURCE
            else:
                token = resolve_env_credential(env_name) or ""
                if not token:
                    outcome.error = CREDENTIAL_MISSING
                else:
                    async with httpx.AsyncClient(
                        timeout=settings.ingest_timeout_seconds,
                        follow_redirects=False,
                        transport=http_transport,
                    ) as client:
                        api = _Api(client, base, token)
                        await _drain(api, project_id, outcome)
                        listed = await api.list_actions()
                        outcome.actions_seen = len(listed)
                        for action in listed:
                            await _handle(api, project, action, outcome)
                        await _drain(api, project_id, outcome)
                        await _push(api, project, outcome)
        except _Abort as abort:
            outcome.error = abort.code
        except Exception as exc:
            logger.error("triage project=%s tick failed (%s)", project_id, type(exc).__name__)
            outcome.error = INTERNAL_ERROR
        finally:
            token = ""
        outcome.consecutive_failures = db.triage_sync_state_record(
            project_id, error=outcome.error, unresolved_actions=outcome.unresolved
        )
    if outcome.error or outcome.actions_started or outcome.results_pushed:
        logger.info(
            "triage project=%s actions=%d started=%d posted=%d pushed=%d error=%s",
            project_id, outcome.actions_seen, outcome.actions_started,
            outcome.terminals_posted, outcome.results_pushed, outcome.error,
        )
    return outcome


async def run_tick() -> None:
    """Sync every configured project once, honoring per-project backoff."""
    try:
        expire_overdue()  # every tick, even for projects in backoff
    except Exception as exc:
        logger.error("triage expiry sweep failed (%s)", type(exc).__name__)
    for project in db.projects_with_triage_sync():
        pid = project["id"]
        remaining = _skip.get(pid, 0)
        if remaining > 0:
            _skip[pid] = remaining - 1
            try:
                await check_prs_only(pid)  # sonicgrid backs off, GitHub reads do not
            except Exception as exc:
                logger.error("triage project=%s PR check failed (%s)", pid, type(exc).__name__)
            continue
        try:
            outcome = await sync_project(pid)
        except Exception as exc:
            logger.error("triage project=%s tick failed (%s)", pid, type(exc).__name__)
            outcome = TickOutcome(error=INTERNAL_ERROR)
        if outcome.error in _BACKOFF_ERRORS:
            _skip[pid] = min(2 ** max(outcome.consecutive_failures, 1), MAX_BACKOFF_TICKS)
        else:
            _skip.pop(pid, None)


async def _loop() -> None:
    logger.info("Triage sync started (tick=%ds)", settings.triage_sync_seconds)
    while True:
        try:
            await run_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # keep going; never log exception text
            logger.error("triage tick failed (%s)", type(exc).__name__)
        await asyncio.sleep(settings.triage_sync_seconds)


async def wait_idle() -> None:
    """Await every running action task (tests; shutdown)."""
    while True:
        tasks = [h.task for h in _holds.values() if h.task and not h.task.done()]
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


def triage_sync_alive() -> bool:
    return _task is not None and not _task.done()


def start_triage_sync() -> asyncio.Task:
    global _task
    _task = asyncio.create_task(_loop(), name="bugalizer-triage-sync")
    return _task


async def stop_triage_sync() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
    # Running actions stop with the process; recovery picks them up from
    # their tagged rows on the next start.
    for hold in list(_holds.values()):
        if hold.task and not hold.task.done():
            hold.task.cancel()
    _holds.clear()
    logger.info("Triage sync stopped")


def reset_runtime_state() -> None:
    """Forget in-memory backoff, locks and slots (tests model a restart)."""
    _skip.clear()
    _locks.clear()
    _holds.clear()
