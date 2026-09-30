"""Ingest poller (Phase 10 / B1 sonicgrid-ingest).

Pulls bug reports from every project with an `ingest_source` into the queue.
Sonicgrid runs on Vercel and cannot reach BOWIE, so Bugalizer polls its
`GET /api/bugalizer/bug-reports` (sonicgrid `documentation/BUGALIZER-POLL-ENDPOINT.md`).
Nothing is ever written back to the source.

Per project, each poll runs:

1. **Forward walk** from the stored `cursor`, up to `ingest_max_pages` pages.
   Cursor rules from the contract: sent back verbatim, stored only when
   non-null, kept on an empty page, absent = from the beginning.
2. **Reconciliation re-walk** (only when the forward walk succeeded): a second
   walk from no cursor with its own persisted `rewalk_cursor`, up to
   `ingest_rewalk_pages` pages per poll, resumable across polls and restarts.
   It catches the reports a forward-only walk cannot see (late inserts,
   reopened reports) and never touches the forward cursor. Due every
   `ingest_rewalk_hours`, or on demand (`full=True`).
3. **Bookkeeping**: one write for the whole poll — `last_error` +
   `consecutive_failures` on failure, `last_ok_at` and a reset on success.

Every write goes through `db.ingest_commit`, fenced by the project's ingest
generation captured under the per-project lock; a config change mid-poll makes
the rest of the poll a no-op (`config_changed`).

The poll token is resolved by name (`ingest_config.credential_env`) from the
process environment, else the deployment `.env`, at request time and sent only in the Authorization header. It is never stored,
logged or returned, and redirects are not followed.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from bugalizer.config import resolve_env_credential, settings
from bugalizer.db import (
    db_write_lock,
    ingest_commit,
    ingest_state_get,
    project_get,
    projects_with_ingest,
)
from bugalizer.ingest.sonicgrid import PollPage, map_report, parse_page

logger = logging.getLogger(__name__)

# Fixed error vocabulary: `last_error` and logs carry only these (plus an HTTP
# status for unexpected_status), never exception text or response bodies.
UNAUTHORIZED = "unauthorized"
SOURCE_NOT_CONFIGURED = "source_not_configured"
BAD_CURSOR = "bad_cursor"
UPSTREAM_ERROR = "upstream_error"
NETWORK_ERROR = "network_error"
TIMEOUT = "timeout"
MALFORMED_RESPONSE = "malformed_response"
UNEXPECTED_STATUS = "unexpected_status"
CREDENTIAL_MISSING = "credential_missing"
NOT_CONFIGURED = "not_configured"      # outcome only: project has no ingest source
CONFIG_CHANGED = "config_changed"      # outcome only: fenced out mid-poll
INTERNAL_ERROR = "internal_error"      # an unexpected exception inside one project's poll

MAX_BACKOFF_TICKS = 32

# Test seam: tests point the poller at a fake sonicgrid via httpx.MockTransport.
http_transport: Optional[httpx.AsyncBaseTransport] = None

_task: Optional[asyncio.Task] = None
# Per-project poll locks, per event loop (an asyncio.Lock binds to one loop).
_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)
# Background-loop backoff: ticks left to skip per project (in memory only).
_skip: dict[str, int] = {}


class _Stale(Exception):
    """The project's ingest config changed (or it was deleted) mid-poll."""


@dataclass
class PollOutcome:
    imported: int = 0
    pages: int = 0
    skipped: int = 0
    error: Optional[str] = None
    stale: bool = False
    consecutive_failures: int = 0


@dataclass
class _Poll:
    project_id: str
    generation: int
    source: str
    url: str
    token: str = ""
    client: Optional[httpx.AsyncClient] = None
    outcome: PollOutcome = field(default_factory=PollOutcome)

    async def commit(self, *, reports: Optional[list[dict[str, Any]]] = None,
                     updates: Optional[dict[str, Any]] = None,
                     count_failure: bool = False) -> int:
        async with db_write_lock:
            inserted = ingest_commit(
                self.project_id, self.generation,
                reports=reports or (), updates=updates, count_failure=count_failure,
            )
        if inserted is None:
            raise _Stale
        return inserted


def _page_limit() -> int:
    return max(1, min(200, settings.ingest_page_limit))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _project_lock(project_id: str) -> asyncio.Lock:
    per_loop = _locks.setdefault(asyncio.get_running_loop(), {})
    return per_loop.setdefault(project_id, asyncio.Lock())


async def _fetch(poll: _Poll, cursor: Optional[str]) -> tuple[Optional[PollPage], Optional[str]]:
    """One page, or a fixed error code."""
    assert poll.client is not None
    params = {"limit": str(_page_limit())}
    if cursor is not None:
        params["cursor"] = cursor
    try:
        resp = await poll.client.get(
            poll.url, params=params, headers={"Authorization": f"Bearer {poll.token}"}
        )
    except httpx.TimeoutException:
        return None, TIMEOUT
    except httpx.HTTPError:
        return None, NETWORK_ERROR
    status = resp.status_code
    if status == 200:
        try:
            body = resp.json()
        except ValueError:
            return None, MALFORMED_RESPONSE
        page = parse_page(body)
        return (page, None) if page is not None else (None, MALFORMED_RESPONSE)
    if status == 400:
        return None, BAD_CURSOR
    if status == 401:
        return None, UNAUTHORIZED
    if status == 503:
        return None, SOURCE_NOT_CONFIGURED
    if 500 <= status < 600:
        return None, UPSTREAM_ERROR
    return None, f"{UNEXPECTED_STATUS}:{status}"


def _map_page(poll: _Poll, page: PollPage) -> list[dict[str, Any]]:
    mapped = []
    for raw in page.reports:
        row = map_report(raw, poll.source)
        if row is None:
            poll.outcome.skipped += 1
        else:
            mapped.append(row)
    return mapped


def _walk_ends(page: PollPage) -> bool:
    """No further page: a null cursor, or a short page (the next one would be
    the empty `[]`/null page)."""
    return page.next_cursor is None or len(page.reports) < _page_limit()


async def _forward(poll: _Poll, cursor: Optional[str]) -> None:
    for _ in range(settings.ingest_max_pages):
        page, error = await _fetch(poll, cursor)
        if error is not None:
            if error == BAD_CURSOR:
                # Contract rule 4: no cursor = from the beginning; imports are
                # idempotent, so a rewind costs one re-walk and nothing else.
                await poll.commit(updates={"cursor": None})
            poll.outcome.error = error
            return
        assert page is not None
        updates = {"cursor": page.next_cursor} if page.next_cursor is not None else {}
        poll.outcome.imported += await poll.commit(reports=_map_page(poll, page), updates=updates)
        poll.outcome.pages += 1
        if _walk_ends(page):
            return
        cursor = page.next_cursor


def _rewalk_due(state: Optional[dict[str, Any]]) -> bool:
    last = (state or {}).get("last_full_walk_at")
    if not last:
        return False
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - last_dt >= timedelta(hours=settings.ingest_rewalk_hours)


async def _rewalk(poll: _Poll, full: bool) -> None:
    state = ingest_state_get(poll.project_id) or {}
    started = state.get("rewalk_started_at")
    if started is None:
        if not (full or _rewalk_due(state)):
            return
        started = _now_iso()
        await poll.commit(updates={"rewalk_started_at": started, "rewalk_cursor": None})
        cursor = None
    else:
        cursor = state.get("rewalk_cursor")
    for _ in range(settings.ingest_rewalk_pages):
        page, error = await _fetch(poll, cursor)
        if error is not None:
            if error == BAD_CURSOR:
                # Restart this re-walk from the beginning; it stays in progress
                # and the forward cursor is untouched.
                await poll.commit(updates={"rewalk_cursor": None})
            poll.outcome.error = error
            return
        assert page is not None
        done = _walk_ends(page)
        updates: dict[str, Any] = {}
        if page.next_cursor is not None:
            updates["rewalk_cursor"] = page.next_cursor
        if done:
            # The start time, so anything reopened during a long walk is
            # covered by the next one.
            updates.update(last_full_walk_at=started, rewalk_started_at=None, rewalk_cursor=None)
        poll.outcome.imported += await poll.commit(reports=_map_page(poll, page), updates=updates)
        poll.outcome.pages += 1
        if done:
            return
        cursor = page.next_cursor


async def _finish(poll: _Poll) -> None:
    now = _now_iso()
    if poll.outcome.error is not None:
        await poll.commit(
            updates={"last_poll_at": now, "last_error": poll.outcome.error}, count_failure=True
        )
    else:
        await poll.commit(updates={
            "last_poll_at": now, "last_ok_at": now,
            "last_error": None, "consecutive_failures": 0,
        })
    state = ingest_state_get(poll.project_id) or {}
    poll.outcome.consecutive_failures = int(state.get("consecutive_failures") or 0)


async def poll_project(project_id: str, *, full: bool = False) -> PollOutcome:
    """Poll one project now: forward walk, re-walk if due (or `full`), then
    bookkeeping. Serialized per project; config is read under the lock."""
    async with _project_lock(project_id):
        project = project_get(project_id)
        config = (project or {}).get("ingest_config")
        if not project or not project.get("ingest_source") or not isinstance(config, dict):
            return PollOutcome(error=NOT_CONFIGURED)
        poll = _Poll(
            project_id=project_id,
            generation=int(project.get("ingest_generation") or 0),
            source=project["ingest_source"],
            url=config["url"],
        )
        try:
            poll.token = resolve_env_credential(config["credential_env"]) or ""
            if not poll.token:
                poll.outcome.error = CREDENTIAL_MISSING
            else:
                state = ingest_state_get(project_id)
                if state and state.get("generation") != poll.generation:
                    state = None
                async with httpx.AsyncClient(
                    timeout=settings.ingest_timeout_seconds,
                    follow_redirects=False,
                    transport=http_transport,
                ) as client:
                    poll.client = client
                    await _forward(poll, (state or {}).get("cursor"))
                    if poll.outcome.error is None:
                        await _rewalk(poll, full)
            await _finish(poll)
        except _Stale:
            poll.outcome.error = CONFIG_CHANGED
            poll.outcome.stale = True
        except Exception as exc:
            # Contain it to this project: the failed step's transaction has
            # rolled back; record a fixed error (never exception text) through
            # the same generation fence so backoff applies, and let the tick
            # move on. CancelledError is not an Exception and still propagates.
            logger.error("ingest project=%s poll failed (%s)", project_id, type(exc).__name__)
            poll.outcome.error = INTERNAL_ERROR
            try:
                await _finish(poll)
            except _Stale:
                poll.outcome.error = CONFIG_CHANGED
                poll.outcome.stale = True
            except Exception as book_exc:
                logger.error(
                    "ingest project=%s could not record failure (%s)",
                    project_id, type(book_exc).__name__,
                )
        finally:
            poll.token = ""
        outcome = poll.outcome
    if outcome.skipped:
        logger.warning("ingest project=%s skipped %d unmappable report(s)", project_id, outcome.skipped)
    if outcome.imported or outcome.error:
        logger.info(
            "ingest project=%s imported=%d pages=%d error=%s",
            project_id, outcome.imported, outcome.pages, outcome.error,
        )
    return outcome


async def run_tick() -> None:
    """Poll every configured project once, honoring per-project backoff."""
    for project in projects_with_ingest():
        pid = project["id"]
        remaining = _skip.get(pid, 0)
        if remaining > 0:
            _skip[pid] = remaining - 1
            continue
        try:
            outcome = await poll_project(pid)
        except Exception as exc:  # second boundary, e.g. the project read itself failed
            logger.error("ingest project=%s poll failed (%s)", pid, type(exc).__name__)
            outcome = PollOutcome(error=INTERNAL_ERROR)
        if outcome.error is not None and not outcome.stale:
            # Whole-poll failure count: a forward success does not reset a
            # repeated re-walk failure (bookkeeping is one write per poll).
            _skip[pid] = min(2 ** max(outcome.consecutive_failures, 1), MAX_BACKOFF_TICKS)
        else:
            _skip.pop(pid, None)


async def _loop() -> None:
    logger.info("Ingest poller started (poll=%ds)", settings.ingest_poll_seconds)
    while True:
        try:
            await run_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # keep polling; never log exception text
            logger.error("ingest tick failed (%s)", type(exc).__name__)
        await asyncio.sleep(settings.ingest_poll_seconds)


def ingest_alive() -> bool:
    return _task is not None and not _task.done()


def start_ingest() -> asyncio.Task:
    global _task
    _task = asyncio.create_task(_loop(), name="bugalizer-ingest-poller")
    return _task


async def stop_ingest() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
        logger.info("Ingest poller stopped")


def reset_runtime_state() -> None:
    """Forget in-memory backoff and locks (tests model a process restart)."""
    _skip.clear()
    _locks.clear()
