"""Phase 10 (B1 sonicgrid-ingest): the ingest poller against a fake sonicgrid.

`FakeSonicgrid` implements the poll contract (sonicgrid
`documentation/BUGALIZER-POLL-ENDPOINT.md`): bearer auth, an opaque composite
`(createdAt, id)` cursor, pages strictly after it, `next_cursor` null on an
empty page, 400 for a cursor that does not decode. It is wired in through
httpx.MockTransport, so every request the poller makes is recorded.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sqlite3
import subprocess
import sys
import textwrap
from typing import Any, Callable, Optional

import httpx
import pytest
from fastapi.testclient import TestClient

os.environ["BUGALIZER_DB_PATH"] = ":memory:"
os.environ["BUGALIZER_QUEUE_ENABLED"] = "false"

from bugalizer import db  # noqa: E402
from bugalizer.config import Settings, resolve_env_credential, settings  # noqa: E402
from bugalizer.ingest import poller  # noqa: E402
from bugalizer.ingest.sonicgrid import (  # noqa: E402
    DESCRIPTION_MAX,
    TRUNCATION_MARKER,
    derive_title,
    map_report,
)
from bugalizer.main import app  # noqa: E402

TOKEN = "sgtok-PLANTED-7f3a9c1e5b2d4f60a8c7e9d1b3f5a7c9"
EMAIL = "planted.reporter+b1@example.invalid"
URL = "https://sonicgrid.test/api/bugalizer/bug-reports"
CONFIG = {"url": URL, "table": "bug_reports", "credential_env": "SONICGRID_POLL_TOKEN"}


def ts(n: int, frac: int = 0) -> str:
    return f"2026-09-30T04:{n // 60:02d}:{n % 60:02d}.{frac:06d}+00:00"


class FakeSonicgrid:
    def __init__(self) -> None:
        self.reports: list[dict[str, Any]] = []
        self.requests: list[dict[str, Optional[str]]] = []
        self.rules: list[tuple[Callable[[httpx.Request], bool], Callable[[httpx.Request], httpx.Response]]] = []
        self.hold: Optional[asyncio.Event] = None
        self.entered: Optional[asyncio.Event] = None

    @staticmethod
    def encode(created: str, rid: str) -> str:
        return base64.urlsafe_b64encode(f"{created}|{rid}".encode()).decode().rstrip("=")

    def add(self, rid: str, created: str, description: str = "Play button does nothing\nsteps...",
            name: str = "Greg", attachments: Optional[list] = None) -> None:
        self.reports.append({
            "id": rid, "description": description, "status": "active",
            "reporterName": name, "reporterEmail": EMAIL,
            "attachments": attachments if attachments is not None else [],
            "createdAt": created, "updatedAt": created,
        })

    def once(self, predicate: Callable[[httpx.Request], bool],
             respond: Callable[[httpx.Request], httpx.Response]) -> None:
        """Answer the next request matching `predicate` with `respond` (one shot)."""
        self.rules.append((predicate, respond))

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append({
            "cursor": request.url.params.get("cursor"),
            "limit": request.url.params.get("limit"),
            "auth": request.headers.get("authorization"),
            "host": request.url.host,
        })
        if self.entered is not None:
            self.entered.set()
        if self.hold is not None:
            await self.hold.wait()
        for i, (predicate, respond) in enumerate(self.rules):
            if predicate(request):
                del self.rules[i]
                return respond(request)
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": "unauthorized"})
        limit = int(request.url.params.get("limit", "100"))
        cursor = request.url.params.get("cursor")
        items = sorted(self.reports, key=lambda r: (r["createdAt"], r["id"]))
        if cursor is not None:
            try:
                raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
                created, rid = raw.split("|", 1)
            except Exception:
                return httpx.Response(400, json={"error": "invalid cursor"})
            items = [r for r in items if (r["createdAt"], r["id"]) > (created, rid)]
        page = items[:limit]
        nxt = self.encode(page[-1]["createdAt"], page[-1]["id"]) if page else None
        return httpx.Response(200, json={"reports": page, "next_cursor": nxt})


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    db.reset_conn()
    settings.db_path = ":memory:"
    settings.queue_enabled = False
    db.init_db()
    for name, value in {
        "ingest_enabled": False, "ingest_page_limit": 100, "ingest_max_pages": 20,
        "ingest_rewalk_hours": 6.0, "ingest_rewalk_pages": 5, "api_keys": "",
    }.items():
        monkeypatch.setattr(settings, name, value)
    poller.reset_runtime_state()
    yield
    poller.reset_runtime_state()
    db.reset_conn()
    settings.db_path = ":memory:"


@pytest.fixture
def fake(monkeypatch) -> FakeSonicgrid:
    sg = FakeSonicgrid()
    monkeypatch.setattr(poller, "http_transport", httpx.MockTransport(sg.handler))
    monkeypatch.setenv("SONICGRID_POLL_TOKEN", TOKEN)
    return sg


def make_project(name: str = "sonicgrid", config: Optional[dict] = None) -> str:
    return db.project_create(
        name=name, repo_url="https://github.com/spherop/sonicgrid",
        ingest_source="supabase", ingest_config=dict(config or CONFIG),
    )["id"]


def poll(pid: str, **kw) -> poller.PollOutcome:
    return asyncio.run(poller.poll_project(pid, **kw))


def rows(pid: str) -> list[dict]:
    return [dict(r) for r in db._get_conn().execute(
        "SELECT * FROM bug_reports WHERE project_id = ? ORDER BY created_at", (pid,)
    ).fetchall()]


def external_ids(pid: str) -> list[str]:
    return [r["external_id"] for r in rows(pid)]


def state(pid: str) -> dict:
    return db.ingest_state_get(pid) or {}


# ---------------------------------------------------------------------------
# 1-4: idempotency, cursor rules, walks, page cap
# ---------------------------------------------------------------------------

def test_same_page_twice_imports_once(fake):
    pid = make_project()
    for i in range(3):
        fake.add(f"r{i}", ts(i))
    assert poll(pid).imported == 3
    # Rewind the checkpoint (as a lost checkpoint would): the replay is a no-op.
    assert db.ingest_commit(pid, 0, updates={"cursor": None}) == 0
    out = poll(pid)
    assert out.imported == 0 and out.error is None
    assert external_ids(pid) == ["r0", "r1", "r2"]


def test_cursor_rules(fake, monkeypatch):
    monkeypatch.setattr(settings, "ingest_page_limit", 2)
    pid = make_project()
    for i in range(5):
        fake.add(f"r{i}", ts(i))
    poll(pid)
    sent = [r["cursor"] for r in fake.requests]
    assert sent[0] is None                                   # first poll: from the beginning
    assert sent[1:] == [FakeSonicgrid.encode(ts(1), "r1"), FakeSonicgrid.encode(ts(3), "r3")]
    last = FakeSonicgrid.encode(ts(4), "r4")
    assert state(pid)["cursor"] == last                      # stored verbatim
    fake.requests.clear()
    out = poll(pid)                                          # empty page: [] / null
    assert out.imported == 0 and out.error is None
    assert [r["cursor"] for r in fake.requests] == [last]    # sent back byte-equal
    assert state(pid)["cursor"] == last                      # null never overwrites
    assert all(r["auth"] == f"Bearer {TOKEN}" and r["limit"] == "2" for r in fake.requests)


def test_limit_one_walk_same_timestamp(fake, monkeypatch):
    monkeypatch.setattr(settings, "ingest_page_limit", 1)
    pid = make_project()
    for rid in ("d", "a", "c", "b"):
        fake.add(rid, ts(7))                                 # all share createdAt
    out = poll(pid)
    assert out.imported == 4
    assert external_ids(pid) == ["a", "b", "c", "d"]         # (createdAt, id) order


def test_page_cap_spreads_backlog_over_ticks(fake, monkeypatch):
    monkeypatch.setattr(settings, "ingest_page_limit", 2)
    monkeypatch.setattr(settings, "ingest_max_pages", 2)
    pid = make_project()
    for i in range(9):
        fake.add(f"r{i}", ts(i))
    assert [poll(pid).imported for _ in range(4)] == [4, 4, 1, 0]
    ids = external_ids(pid)
    assert sorted(ids) == [f"r{i}" for i in range(9)] and len(set(ids)) == 9


# ---------------------------------------------------------------------------
# 5: crash safety
# ---------------------------------------------------------------------------

def test_failure_mid_page_rolls_back_imports_and_checkpoint(fake, monkeypatch):
    pid = make_project()
    for i in range(3):
        fake.add(f"r{i}", ts(i))
    real_new_id = db._new_id
    calls = {"n": 0}

    def flaky_new_id() -> str:
        calls["n"] += 1
        if calls["n"] == 2:                                  # second row of the page
            raise RuntimeError("boom")
        return real_new_id()

    monkeypatch.setattr(db, "_new_id", flaky_new_id)
    out = poll(pid)                                          # contained, not raised
    assert out.error == "internal_error"
    assert rows(pid) == []                                   # first staged row rolled back
    assert state(pid)["cursor"] is None                      # checkpoint did not advance
    assert state(pid)["last_error"] == "internal_error"
    assert state(pid)["consecutive_failures"] == 1
    monkeypatch.setattr(db, "_new_id", real_new_id)
    assert poll(pid).imported == 3
    assert external_ids(pid) == ["r0", "r1", "r2"]


# ---------------------------------------------------------------------------
# 6: reconciliation re-walk
# ---------------------------------------------------------------------------

def _use_file_db(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "ingest.db"))
    db.reset_conn()
    db.init_db()


def _restart() -> None:
    """Model a process restart: drop every cached connection and all poller
    in-memory state; only the DB file survives."""
    db.reset_conn()
    poller.reset_runtime_state()


def test_rewalk_capped_resumes_after_restart_forward_cursor_unchanged(fake, monkeypatch, tmp_path):
    _use_file_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "ingest_page_limit", 2)
    monkeypatch.setattr(settings, "ingest_rewalk_pages", 2)
    pid = make_project()
    for i in range(10):
        fake.add(f"r{i:02d}", ts(10 * i))
    assert poll(pid).imported == 10
    forward = state(pid)["cursor"]
    assert forward == FakeSonicgrid.encode(ts(90), "r09")

    # A late insert lands behind the checkpoint, past the first re-walk chunk
    # (4 reports): between r06 and r07.
    fake.add("late", ts(65))
    assert poll(pid).imported == 0                          # forward walk cannot see it
    assert state(pid)["cursor"] == forward

    fake.requests.clear()
    out = poll(pid, full=True)                               # re-walk: chunk 1 of 3
    assert out.imported == 0 and out.error is None
    s1 = state(pid)
    started = s1["rewalk_started_at"]
    assert started is not None
    assert s1["rewalk_cursor"] == FakeSonicgrid.encode(ts(30), "r03")
    assert s1["cursor"] == forward
    assert "late" not in external_ids(pid)

    _restart()

    fake.requests.clear()
    out = poll(pid)                                          # chunk 2: resumes, imports late
    rewalk_sent = [r["cursor"] for r in fake.requests if r["cursor"] != forward]
    assert rewalk_sent[0] == FakeSonicgrid.encode(ts(30), "r03")   # no page before position
    assert None not in rewalk_sent
    assert out.imported == 1
    assert external_ids(pid).count("late") == 1
    assert state(pid)["cursor"] == forward
    assert state(pid)["rewalk_started_at"] == started

    _restart()

    out = poll(pid)                                          # chunk 3: completes
    s3 = state(pid)
    assert out.imported == 0 and out.error is None
    assert s3["rewalk_started_at"] is None and s3["rewalk_cursor"] is None
    assert s3["last_full_walk_at"] == started                # the walk's start time
    assert s3["cursor"] == forward                           # byte-equal throughout
    assert len(external_ids(pid)) == 11


def test_new_report_imported_by_forward_poll_mid_rewalk(fake, monkeypatch):
    monkeypatch.setattr(settings, "ingest_page_limit", 2)
    monkeypatch.setattr(settings, "ingest_rewalk_pages", 1)
    pid = make_project()
    for i in range(6):
        fake.add(f"r{i}", ts(i))
    poll(pid)
    poll(pid, full=True)                                     # re-walk in progress
    assert state(pid)["rewalk_started_at"] is not None
    fake.add("new", ts(30))
    assert poll(pid).imported == 1                           # forward, before re-walk ends
    assert state(pid)["cursor"] == FakeSonicgrid.encode(ts(30), "new")
    assert state(pid)["rewalk_started_at"] is not None       # still reconciling


def test_rewalk_due_by_interval_and_first_walk_does_not_trigger_one(fake, monkeypatch):
    pid = make_project()
    fake.add("r0", ts(0))
    poll(pid)
    assert state(pid)["rewalk_started_at"] is None           # creation counts as a full walk
    db.ingest_commit(pid, 0, updates={"last_full_walk_at": "2026-01-01T00:00:00+00:00"})
    fake.requests.clear()
    poll(pid)
    assert [r["cursor"] for r in fake.requests][-1] is None  # re-walk from the beginning
    assert state(pid)["last_full_walk_at"] > "2026-09"       # completed


def test_rewalk_errors_keep_position_and_400_clears_only_its_cursor(fake, monkeypatch):
    monkeypatch.setattr(settings, "ingest_page_limit", 2)
    monkeypatch.setattr(settings, "ingest_rewalk_pages", 1)
    pid = make_project()
    for i in range(6):
        fake.add(f"r{i}", ts(i))
    poll(pid)
    forward = state(pid)["cursor"]
    poll(pid, full=True)
    position = state(pid)["rewalk_cursor"]
    assert position is not None

    fake.once(lambda r: r.url.params.get("cursor") == position, lambda r: httpx.Response(502))
    out = poll(pid)
    assert out.error == "upstream_error"
    s = state(pid)
    assert s["rewalk_cursor"] == position and s["rewalk_started_at"] is not None
    assert s["cursor"] == forward and s["last_error"] == "upstream_error"

    fake.once(lambda r: r.url.params.get("cursor") == position, lambda r: httpx.Response(400))
    assert poll(pid).error == "bad_cursor"
    s = state(pid)
    assert s["rewalk_cursor"] is None and s["rewalk_started_at"] is not None
    assert s["cursor"] == forward

    fake.once(lambda r: r.url.params.get("cursor") == forward, lambda r: httpx.Response(400))
    assert poll(pid).error == "bad_cursor"
    s = state(pid)
    assert s["cursor"] is None and s["rewalk_started_at"] is not None


# ---------------------------------------------------------------------------
# 6b: a stale in-flight poll cannot write
# ---------------------------------------------------------------------------

def _held_poll(fake: FakeSonicgrid, pid: str, change: Callable[[], None],
               respond: Optional[Callable[[httpx.Request], httpx.Response]] = None) -> poller.PollOutcome:
    async def scenario() -> poller.PollOutcome:
        fake.hold, fake.entered = asyncio.Event(), asyncio.Event()
        if respond is not None:
            fake.once(lambda r: True, respond)
        task = asyncio.create_task(poller.poll_project(pid))
        await fake.entered.wait()                            # request is in flight
        change()
        fake.hold.set()
        return await task

    try:
        return asyncio.run(scenario())
    finally:
        fake.hold = fake.entered = None


@pytest.mark.parametrize("error_response", [False, True])
@pytest.mark.parametrize("case", ["config_changed", "cleared", "deleted"])
def test_stale_in_flight_poll_writes_nothing(fake, case, error_response):
    pid = make_project()
    fake.add("r0", ts(0))
    changes = {
        "config_changed": lambda: db.project_update(
            pid, ingest_config={**CONFIG, "url": "https://sonicgrid.test/api/v2/bug-reports"}),
        "cleared": lambda: db.project_update(pid, ingest_source=None, ingest_config=None),
        "deleted": lambda: db.project_delete(pid),
    }
    out = _held_poll(fake, pid, changes[case],
                     (lambda r: httpx.Response(502)) if error_response else None)
    assert out.stale and out.error == "config_changed"
    assert rows(pid) == []
    assert db.ingest_state_get(pid) is None                  # no stale cursor or last_error

    if case == "config_changed":
        fake.requests.clear()
        assert poll(pid).imported == 1                       # new generation starts fresh
        assert fake.requests[0]["cursor"] is None
        assert state(pid)["generation"] == 1


# ---------------------------------------------------------------------------
# 6c: deletion
# ---------------------------------------------------------------------------

def test_delete_polled_project_with_no_imports(fake):
    pid = make_project()
    poll(pid)                                                # state row, zero reports
    assert state(pid) != {}
    assert db.project_delete(pid) is True
    assert db.ingest_state_get(pid) is None


def test_delete_project_whose_imports_were_soft_deleted(fake):
    pid = make_project()
    fake.add("r0", ts(0))
    poll(pid)
    assert db.project_delete(pid) == "has_reports"           # active import blocks delete
    db.report_delete(rows(pid)[0]["id"])
    assert db.project_delete(pid) is True
    assert db.ingest_state_get(pid) is None
    assert rows(pid) == []


# ---------------------------------------------------------------------------
# 7-8: errors, backoff, credential
# ---------------------------------------------------------------------------

def _raise(exc_type):
    def respond(request):
        raise exc_type("simulated", request=request)
    return respond


@pytest.mark.parametrize("respond, code", [
    (lambda r: httpx.Response(401), "unauthorized"),
    (lambda r: httpx.Response(503), "source_not_configured"),
    (lambda r: httpx.Response(502), "upstream_error"),
    (_raise(httpx.ReadTimeout), "timeout"),
    (_raise(httpx.ConnectError), "network_error"),
    (lambda r: httpx.Response(302, headers={"location": "https://evil.test/steal"}),
     "unexpected_status:302"),
    (lambda r: httpx.Response(200, text="<html>not json</html>"), "malformed_response"),
    (lambda r: httpx.Response(200, json={"items": []}), "malformed_response"),
])
def test_error_responses_record_fixed_code_and_keep_checkpoint(fake, respond, code):
    pid = make_project()
    fake.add("r0", ts(0))
    poll(pid)
    cursor = state(pid)["cursor"]
    fake.once(lambda r: True, respond)
    out = poll(pid)
    assert out.error == code
    s = state(pid)
    assert s["last_error"] == code and s["cursor"] == cursor
    assert s["consecutive_failures"] == 1
    assert all(r["host"] == "sonicgrid.test" for r in fake.requests)   # redirect not followed


def test_unmappable_report_is_skipped_without_blocking_page(fake):
    pid = make_project()
    fake.add("r0", ts(0))
    fake.reports.append({"description": "no id", "createdAt": ts(1)})
    fake.reports[-1]["id"] = ""                              # sorts, but fails mapping
    fake.add("r2", ts(2))
    out = poll(pid)
    assert out.error is None and out.skipped == 1
    assert external_ids(pid) == ["r0", "r2"]


def test_credential_missing_makes_no_request(fake, monkeypatch):
    monkeypatch.delenv("SONICGRID_POLL_TOKEN")
    pid = make_project()
    out = poll(pid)
    assert out.error == "credential_missing"
    assert fake.requests == []
    assert state(pid)["last_error"] == "credential_missing"


def test_tick_isolates_failing_project_and_backs_off(fake, monkeypatch):
    good = make_project("good")
    bad = make_project("bad", {**CONFIG, "credential_env": "OTHER_TOKEN"})
    fake.add("r0", ts(0))
    asyncio.run(poller.run_tick())
    assert len(rows(good)) == 1                              # bad did not stop good
    assert state(bad)["last_error"] == "credential_missing"
    assert poller._skip[bad] == 2                            # 2^1 ticks
    asyncio.run(poller.run_tick())
    asyncio.run(poller.run_tick())
    assert state(bad)["consecutive_failures"] == 1           # skipped twice
    asyncio.run(poller.run_tick())
    assert state(bad)["consecutive_failures"] == 2
    assert poller._skip[bad] == 4
    monkeypatch.setenv("OTHER_TOKEN", TOKEN)
    poller._skip[bad] = 0
    asyncio.run(poller.run_tick())
    assert state(bad)["consecutive_failures"] == 0 and state(bad)["last_error"] is None
    assert bad not in poller._skip


def test_forward_success_does_not_reset_repeated_rewalk_failure(fake, monkeypatch):
    monkeypatch.setattr(settings, "ingest_page_limit", 2)
    monkeypatch.setattr(settings, "ingest_rewalk_pages", 1)
    pid = make_project()
    for i in range(4):
        fake.add(f"r{i}", ts(i))
    poll(pid)
    poll(pid, full=True)
    position = state(pid)["rewalk_cursor"]
    for expected in (1, 2):
        fake.once(lambda r: r.url.params.get("cursor") == position, lambda r: httpx.Response(502))
        poll(pid)
        assert state(pid)["consecutive_failures"] == expected


# ---------------------------------------------------------------------------
# 9: secrecy
# ---------------------------------------------------------------------------

def _db_dump() -> str:
    conn = db._get_conn()
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    return "\n".join(
        repr(tuple(row)) for t in tables for row in conn.execute(f"SELECT * FROM {t}").fetchall()
    )


def test_token_and_email_never_stored_logged_or_returned(fake, caplog):
    caplog.set_level(logging.DEBUG)
    client = TestClient(app)
    pid = make_project()
    fake.add("r0", ts(0), attachments=[{"url": "https://cdn.test/a.png", "fileName": "a.png",
                                        "contentType": "image/png"}])
    texts = [client.post(f"/api/v1/projects/{pid}/ingest/run").text]
    for respond in (lambda r: httpx.Response(401), lambda r: httpx.Response(502),
                    lambda r: httpx.Response(302, headers={"location": "https://evil.test/"}),
                    _raise(httpx.ConnectError)):
        fake.once(lambda r: True, respond)
        texts.append(client.post(f"/api/v1/projects/{pid}/ingest/run").text)
    texts += [
        client.get(f"/api/v1/projects/{pid}/ingest").text,
        client.get("/health").text,
        client.get("/api/v1/reports").text,
        client.get(f"/api/v1/projects/{pid}").text,
    ]
    dump = _db_dump()
    for secret in (TOKEN, EMAIL):
        assert secret not in dump
        assert secret not in caplog.text
        for text in texts:
            assert secret not in text
    assert "r0" in dump                                      # the scan did see the import


# ---------------------------------------------------------------------------
# 10: mapping
# ---------------------------------------------------------------------------

def test_derive_title():
    assert derive_title("  Play   button\tbroken \n\nmore") == "Play button broken"
    assert derive_title("\n\n   \nsecond line wins") == "second line wins"
    assert derive_title("") == "(no description)"
    assert derive_title("   \n  ") == "(no description)"
    long = "word " * 40
    title = derive_title(long)
    assert title.endswith("…") and len(title) <= 81 and not title[:-1].endswith(" ")
    assert derive_title("x" * 200) == "x" * 80 + "…"         # no space: hard cut


def test_map_report_fields_and_fallbacks():
    raw = {
        "id": "abc", "description": "d" * (DESCRIPTION_MAX + 10), "reporterName": "  ",
        "reporterEmail": EMAIL, "status": "active",
        "attachments": [
            {"url": "https://cdn.test/ok.png", "fileName": "ok.png", "contentType": "image/png"},
            {"url": "http://cdn.test/plain.png"}, {"url": "javascript:alert(1)"}, {"nope": 1},
        ],
    }
    row = map_report(raw, "supabase")
    assert row["external_id"] == "abc" and row["ingest_source"] == "supabase"
    assert len(row["description"]) == DESCRIPTION_MAX
    assert row["description"].endswith(TRUNCATION_MARKER)
    assert row["reporter"] == "sonicgrid user"
    assert row["attachments"] == [
        {"url": "https://cdn.test/ok.png", "fileName": "ok.png", "contentType": "image/png"}
    ]
    assert row["labels"] == ["sonicgrid"] and row["severity"] == "medium"
    assert EMAIL not in json.dumps(row)
    assert map_report({"description": "no id"}, "supabase") is None
    empty = map_report({"id": "e", "description": ""}, "supabase")
    assert empty["description"] == "(empty report)" and empty["title"] == "(no description)"
    assert map_report({"id": "n", "reporterName": "x" * 300}, "supabase")["reporter"] == "x" * 200


# ---------------------------------------------------------------------------
# 11-12: pipeline hand-off, config changes
# ---------------------------------------------------------------------------

def test_imported_report_enters_stage_one_and_api(fake):
    pid = make_project()
    fake.add("r0", ts(0), description="Waveform freezes\nrepro: open track",
             attachments=[{"url": "https://cdn.test/s.png", "fileName": "s.png",
                           "contentType": "image/png"}])
    poll(pid)
    report = rows(pid)[0]
    assert report["status"] == "submitted" and report["analysis_mode"] == "auto"
    assert report["id"] in {r["id"] for r in db.submitted_reports()}
    body = TestClient(app).get(f"/api/v1/reports/{report['id']}").json()
    assert body["title"] == "Waveform freezes"
    assert body["reporter"] == "Greg"
    assert body["labels"] == ["sonicgrid"]
    assert body["external_id"] == "r0" and body["ingest_source"] == "supabase"
    assert body["attachments"][0]["url"] == "https://cdn.test/s.png"


def test_config_change_resets_state_identical_patch_does_not(fake):
    pid = make_project()
    fake.add("r0", ts(0))
    poll(pid)
    client = TestClient(app)
    r = client.patch(f"/api/v1/projects/{pid}", json={"ingest_config": dict(CONFIG)})
    assert r.status_code == 200
    assert state(pid)["cursor"] is not None                  # same config: kept
    r = client.patch(f"/api/v1/projects/{pid}",
                     json={"ingest_config": {**CONFIG, "credential_env": "NEW_TOKEN"}})
    assert r.status_code == 200
    assert db.ingest_state_get(pid) is None
    assert db.project_get(pid)["ingest_generation"] == 1


def test_cleared_ingest_is_no_longer_polled(fake):
    pid = make_project()
    poll(pid)
    db.project_update(pid, ingest_source=None, ingest_config=None)
    fake.requests.clear()
    asyncio.run(poller.run_tick())
    assert fake.requests == []
    assert db.ingest_state_get(pid) is None


def test_migration_adds_ingest_columns_and_index():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE bug_reports (id TEXT PRIMARY KEY, project_id TEXT)")
    conn.execute("CREATE TABLE fix_proposals (id TEXT PRIMARY KEY)")
    db._migrate(conn)
    reports = {r[1] for r in conn.execute("PRAGMA table_info(bug_reports)")}
    projects = {r[1] for r in conn.execute("PRAGMA table_info(projects)")}
    assert {"ingest_source", "external_id"} <= reports
    assert "ingest_generation" in projects
    indexes = {r[1] for r in conn.execute("PRAGMA index_list(bug_reports)")}
    assert "idx_bug_reports_external" in indexes


# ---------------------------------------------------------------------------
# 13-13a: disabled by default, auth, health
# ---------------------------------------------------------------------------

def test_poller_off_by_default_manual_run_still_works(fake):
    pid = make_project()
    fake.add("r0", ts(0))
    with TestClient(app) as client:
        assert poller.ingest_alive() is False
        r = client.post(f"/api/v1/projects/{pid}/ingest/run")
    assert r.status_code == 200
    assert r.json() == {"imported": 1, "pages": 1, "last_error": None}


def test_poller_starts_when_enabled(fake, monkeypatch):
    monkeypatch.setattr(settings, "ingest_enabled", True)
    monkeypatch.setattr(settings, "ingest_poll_seconds", 3600)
    with TestClient(app):
        assert poller.ingest_alive() is True
    assert poller.ingest_alive() is False


def test_ingest_endpoints_require_api_key(fake, monkeypatch):
    pid = make_project()
    monkeypatch.setattr(settings, "api_keys", "k-one")
    client = TestClient(app)
    assert client.get(f"/api/v1/projects/{pid}/ingest").status_code == 401
    assert client.post(f"/api/v1/projects/{pid}/ingest/run").status_code == 401
    headers = {"X-API-Key": "k-one"}
    assert client.get(f"/api/v1/projects/{pid}/ingest", headers=headers).status_code == 200
    assert client.post(f"/api/v1/projects/{pid}/ingest/run", headers=headers).status_code == 200


def test_ingest_endpoints_404_without_ingest_source(fake):
    pid = db.project_create(name="plain", repo_url="https://github.com/x/y")["id"]
    client = TestClient(app)
    assert client.get(f"/api/v1/projects/{pid}/ingest").status_code == 404
    assert client.post(f"/api/v1/projects/{pid}/ingest/run").status_code == 404
    assert client.get("/api/v1/projects/nope/ingest").status_code == 404


def test_status_endpoint_reports_presence_only(fake):
    pid = make_project()
    fake.add("r0", ts(0))
    poll(pid)
    body = TestClient(app).get(f"/api/v1/projects/{pid}/ingest").json()
    assert body["credential_present"] is True and body["cursor_present"] is True
    assert body["imported_total"] == 1 and body["last_error"] is None
    assert body["rewalk_in_progress"] is False and body["enabled"] is False
    assert state(pid)["cursor"] not in json.dumps(body)


def test_health_ingest_block_is_aggregate_and_never_degrades(fake):
    client = TestClient(app)
    before = client.get("/health")
    pid = make_project()
    fake.once(lambda r: True, lambda r: httpx.Response(401))
    poll(pid)
    after = client.get("/health")
    assert after.status_code == before.status_code
    assert after.json()["status"] == before.json()["status"]
    assert after.json()["ingest"] == {"enabled": False, "projects": 1, "failing": 1}
    assert pid not in after.text and "unauthorized" not in after.text


# ---------------------------------------------------------------------------
# impl r1: per-project failure containment (codex P2)
# ---------------------------------------------------------------------------

def test_malformed_attachment_url_is_dropped():
    row = map_report({"id": "x", "attachments": [
        {"url": "https://[broken"}, {"url": "https://cdn.test/ok.png"}]}, "supabase")
    assert row["attachments"] == [{"url": "https://cdn.test/ok.png", "fileName": None,
                                   "contentType": None}]


def _two_hosts(monkeypatch, bad: FakeSonicgrid, good: FakeSonicgrid) -> None:
    async def dispatch(request: httpx.Request) -> httpx.Response:
        return await (bad if request.url.host == "bad.test" else good).handler(request)
    monkeypatch.setattr(poller, "http_transport", httpx.MockTransport(dispatch))
    monkeypatch.setenv("SONICGRID_POLL_TOKEN", TOKEN)


def test_malformed_url_page_bad_project_first_does_not_starve_next(monkeypatch):
    bad_sg, good_sg = FakeSonicgrid(), FakeSonicgrid()
    _two_hosts(monkeypatch, bad_sg, good_sg)
    bad = make_project("bad", {**CONFIG, "url": "https://bad.test/api/bugalizer/bug-reports"})
    good = make_project("good")
    bad_sg.add("b0", ts(0), attachments=[{"url": "https://[broken", "fileName": "x.png"}])
    good_sg.add("g0", ts(0))
    asyncio.run(poller.run_tick())
    assert external_ids(bad) == ["b0"] and rows(bad)[0]["attachments"] is None
    assert external_ids(good) == ["g0"]
    assert state(bad)["last_error"] is None and state(good)["last_error"] is None


def test_unexpected_failure_is_contained_to_its_project(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    bad_sg, good_sg = FakeSonicgrid(), FakeSonicgrid()
    _two_hosts(monkeypatch, bad_sg, good_sg)
    bad = make_project("bad", {**CONFIG, "url": "https://bad.test/api/bugalizer/bug-reports"})
    good = make_project("good")
    bad_sg.add("poison", ts(0))
    good_sg.add("g0", ts(0))
    real_map = poller.map_report

    def exploding_map(raw, source):
        if isinstance(raw, dict) and raw.get("id") == "poison":
            raise ValueError(f"secret-ish detail {TOKEN}")
        return real_map(raw, source)

    monkeypatch.setattr(poller, "map_report", exploding_map)
    asyncio.run(poller.run_tick())                           # does not raise
    assert external_ids(good) == ["g0"]                      # later project still ran
    s = state(bad)
    assert s["last_error"] == "internal_error" and s["consecutive_failures"] == 1
    assert s["cursor"] is None                               # poison page not skipped
    assert poller._skip[bad] == 2                            # backoff applies
    assert TOKEN not in caplog.text and "secret-ish" not in caplog.text
    r = TestClient(app).post(f"/api/v1/projects/{bad}/ingest/run")
    assert r.status_code == 200 and r.json()["last_error"] == "internal_error"


# ---------------------------------------------------------------------------
# impl r1: credential from the deployment .env (codex P1)
# ---------------------------------------------------------------------------

def _env_file(tmp_path, **values: str) -> str:
    path = tmp_path / "deploy.env"
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    return str(path)


def test_dotenv_with_poll_token_starts_and_resolves_credential(fake, monkeypatch, tmp_path):
    monkeypatch.delenv("SONICGRID_POLL_TOKEN")
    path = _env_file(tmp_path, BUGALIZER_INGEST_ENABLED="true", SONICGRID_POLL_TOKEN=TOKEN)
    loaded = Settings(_env_file=path)                        # used to raise extra_forbidden
    assert loaded.ingest_enabled is True
    assert TOKEN not in repr(loaded.model_dump()) and TOKEN not in repr(loaded)
    monkeypatch.setenv("BUGALIZER_ENV_FILE", path)
    assert resolve_env_credential("SONICGRID_POLL_TOKEN") == TOKEN
    assert "SONICGRID_POLL_TOKEN" not in os.environ          # never copied into the process env
    pid = make_project()
    fake.add("r0", ts(0))
    client = TestClient(app)
    assert client.get(f"/api/v1/projects/{pid}/ingest").json()["credential_present"] is True
    assert client.post(f"/api/v1/projects/{pid}/ingest/run").json()["imported"] == 1
    assert fake.requests[-1]["auth"] == f"Bearer {TOKEN}"


def test_dotenv_blank_token_loads_and_reports_missing(fake, monkeypatch, tmp_path):
    monkeypatch.delenv("SONICGRID_POLL_TOKEN")
    path = _env_file(tmp_path, SONICGRID_POLL_TOKEN="")
    Settings(_env_file=path)
    monkeypatch.setenv("BUGALIZER_ENV_FILE", path)
    assert resolve_env_credential("SONICGRID_POLL_TOKEN") is None
    assert poll(make_project()).error == "credential_missing"
    assert fake.requests == []


def test_real_environment_wins_over_dotenv(monkeypatch, tmp_path):
    path = _env_file(tmp_path, SONICGRID_POLL_TOKEN="from-dotenv")
    monkeypatch.setenv("BUGALIZER_ENV_FILE", path)
    monkeypatch.setenv("SONICGRID_POLL_TOKEN", "from-process-env")
    assert resolve_env_credential("SONICGRID_POLL_TOKEN") == "from-process-env"
    monkeypatch.delenv("SONICGRID_POLL_TOKEN")
    assert resolve_env_credential("SONICGRID_POLL_TOKEN") == "from-dotenv"


def test_settings_error_with_dotenv_token_is_secret_free(tmp_path):
    path = _env_file(tmp_path, BUGALIZER_INGEST_POLL_SECONDS="not-a-number",
                     SONICGRID_POLL_TOKEN=TOKEN)
    with pytest.raises(Exception) as err:
        Settings(_env_file=path)
    assert "ingest_poll_seconds" in str(err.value)
    assert TOKEN not in str(err.value)


def test_unknown_bugalizer_keys_in_dotenv_are_named_not_valued(monkeypatch, tmp_path):
    path = _env_file(tmp_path, BUGALIZER_INGEST_ENABLD="true", BUGALIZER_GITHUB_TOKN=TOKEN,
                     SONICGRID_POLL_TOKEN=TOKEN, BUGALIZER_INGEST_ENABLED="true")
    monkeypatch.setenv("BUGALIZER_ENV_FILE", path)
    from bugalizer.config import unknown_env_file_settings
    assert unknown_env_file_settings() == ["BUGALIZER_GITHUB_TOKN", "BUGALIZER_INGEST_ENABLD"]


_STARTUP_SCRIPT = textwrap.dedent("""
    import json, os, sys
    import httpx
    from fastapi.testclient import TestClient
    from bugalizer.main import app
    from bugalizer.ingest import poller

    token = sys.argv[1]
    def handler(request):
        if request.headers.get("authorization") != "Bearer " + token:
            return httpx.Response(401)
        if request.url.params.get("cursor"):
            return httpx.Response(200, json={"reports": [], "next_cursor": None})
        return httpx.Response(200, json={"reports": [{"id": "s1", "description": "Boot bug",
            "reporterName": "Greg", "attachments": [], "createdAt": "t", "updatedAt": "t"}],
            "next_cursor": "c1"})
    poller.http_transport = httpx.MockTransport(handler)
    with TestClient(app) as client:
        pid = client.post("/api/v1/projects", json={
            "name": "sonicgrid", "repo_url": "https://github.com/spherop/sonicgrid",
            "ingest_source": "supabase", "ingest_config": {
                "url": "https://sonicgrid.test/api/bugalizer/bug-reports",
                "table": "bug_reports", "credential_env": "SONICGRID_POLL_TOKEN"}}).json()["id"]
        status = client.get(f"/api/v1/projects/{pid}/ingest").json()
        run = client.post(f"/api/v1/projects/{pid}/ingest/run").json()
        print(json.dumps({"credential_present": status["credential_present"],
                          "imported": run["imported"], "last_error": run["last_error"],
                          "in_environ": "SONICGRID_POLL_TOKEN" in os.environ}))
""")


def test_native_startup_with_deployment_dotenv(tmp_path):
    """A fresh process started the way the native service starts: settings and
    the poll token both come from the deployment .env, nothing from the
    environment. Startup succeeds, the credential resolves, an authorized poll
    imports, and no output carries the token."""
    env_path = _env_file(tmp_path, BUGALIZER_DB_PATH=str(tmp_path / "svc.db"),
                         BUGALIZER_QUEUE_ENABLED="false", SONICGRID_POLL_TOKEN=TOKEN,
                         BUGALIZER_INGEST_ENABLD="true")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("BUGALIZER_", "QA_LLM_", "SONICGRID_"))}
    env["BUGALIZER_ENV_FILE"] = env_path
    proc = subprocess.run([sys.executable, "-c", _STARTUP_SCRIPT, TOKEN], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result == {"credential_present": True, "imported": 1, "last_error": None,
                      "in_environ": False}
    assert TOKEN not in proc.stdout and TOKEN not in proc.stderr
    assert "Ignoring unknown settings in .env: BUGALIZER_INGEST_ENABLD" in proc.stderr
