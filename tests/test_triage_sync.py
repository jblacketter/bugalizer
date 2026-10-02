"""Phase 11 (B3 sonicgrid-triage-sync): the triage sync against a fake sonicgrid.

`FakeTriage` implements sonicgrid's triage contract
(`documentation/BUGALIZER-TRIAGE-ENDPOINTS.md`): bearer auth, the strict result
schema with its limits and the newer-revision-wins rule, the open-action walk
with an opaque `(requestedAt, id)` cursor, the action state machine with
`changed: false` repeats and `409 currentState`, and the `fix_and_open_pr`
step rules. It is wired in through httpx.MockTransport.

The pipeline stages are replaced by `FakeStages` (the executor's module-level
references), which write rows tagged with `trigger_ref` exactly as the real
stages do. The real stages' new parameters (pins, attribution, tags) are
tested directly at the end against a mocked LLM.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from typing import Any, Callable, Optional
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

os.environ["BUGALIZER_DB_PATH"] = ":memory:"
os.environ["BUGALIZER_QUEUE_ENABLED"] = "false"

from bugalizer import db  # noqa: E402
from bugalizer.config import settings  # noqa: E402
from bugalizer.git_ops.pull_request import OpenPrError  # noqa: E402
from bugalizer.llm.client import LLMResponse  # noqa: E402
from bugalizer.main import app  # noqa: E402
from bugalizer.pipeline.fix_proposer import FixOutcome, propose_fix  # noqa: E402
from bugalizer.pipeline.orchestrator import LocalOutcome, process_triaged  # noqa: E402
from bugalizer.sync import actions as ex  # noqa: E402
from bugalizer.sync import triage_sync as ts  # noqa: E402
from bugalizer.sync.results import DIFF_MAX_BYTES, build_payload, fingerprint  # noqa: E402

TOKEN = "sgtriage-PLANTED-3c9e1a7f5b2d4f60a8c7e9d1b3f5a7c9"
JACK = "jack.planted+b3@example.invalid"
DAN = "dan.planted+b3@example.invalid"
POLL_URL = "https://sonicgrid.test/api/bugalizer/bug-reports"
BASE = "https://sonicgrid.test/api/bugalizer"
CONFIG = {
    "url": POLL_URL, "table": "bug_reports", "credential_env": "SONICGRID_POLL_TOKEN",
    "triage_credential_env": "SONICGRID_TRIAGE_TOKEN",
}
STATES = ("pending", "claimed", "done", "failed", "refused")
LEGAL_STEPS = {
    ("done", "done"): "done", ("done", "failed"): "failed", ("done", "refused"): "refused",
    ("failed", "skipped"): "failed", ("refused", "skipped"): "refused",
}
PUBLIC_KEYS = {"pipelineStatus", "summary", "severity", "prUrl"}
ADMIN_KEYS = {
    "analysisMode", "localized", "category", "triageConfidence", "rootCause",
    "rootCauseHypothesis", "explanation", "candidateFiles", "diff", "fixConfidence", "pr",
}


def _schema_error(body: Any) -> Optional[str]:
    """The fake's strict result check (contract "Push a result")."""
    if not isinstance(body, dict) or set(body) != {"revision", "bugalizerUpdatedAt", "public", "admin"}:
        return "top-level keys"
    rev = body["revision"]
    if not isinstance(rev, int) or isinstance(rev, bool) or not 0 <= rev <= 2**53 - 1:
        return "revision"
    pub, adm = body["public"], body["admin"]
    if not isinstance(pub, dict) or set(pub) != PUBLIC_KEYS:
        return "public keys"
    if not isinstance(adm, dict) or set(adm) != ADMIN_KEYS:
        return "admin keys"
    if pub["severity"] not in (None, "critical", "high", "medium", "low"):
        return "severity"
    if pub["summary"] is not None and len(pub["summary"]) > 2000:
        return "summary"
    if adm["category"] is not None and len(adm["category"]) > 50:
        return "category"
    for key in ("triageConfidence", "fixConfidence"):
        v = adm[key]
        if v is not None and not (isinstance(v, (int, float)) and 0 <= v <= 1):
            return key
    files = adm["candidateFiles"]
    if files is not None:
        if len(files) > 50:
            return "candidateFiles length"
        for f in files:
            if set(f) != {"path", "relevance", "reason"} or len(f["path"]) > 500:
                return "candidateFiles item"
    if adm["diff"] is not None and len(adm["diff"].encode()) > 256 * 1024:
        return "diff"
    if adm["pr"] is not None and set(adm["pr"]) != {"number", "branch"}:
        return "pr"
    return None


class FakeTriage:
    def __init__(self) -> None:
        self.actions: dict[str, dict[str, Any]] = {}
        self.results: dict[str, dict[str, Any]] = {}
        self.bugs: set[str] = set()
        self.requests: list[dict[str, Any]] = []
        self.rules: list[tuple[Callable[[httpx.Request], bool], Callable[[httpx.Request], httpx.Response]]] = []
        self.drop_after_commit: set[str] = set()   # POST ids: commit, then lose the response
        self.drop_terminal: set[str] = set()       # same, for a terminal POST only
        self.drop_put_once: set[str] = set()       # PUT bug ids: lose the request entirely
        self._n = 0

    @staticmethod
    def encode(requested: str, aid: str) -> str:
        return base64.urlsafe_b64encode(f"{requested}|{aid}".encode()).decode().rstrip("=")

    def add_action(self, kind: str, bug: str, *, params: Optional[dict] = None,
                   consents: Optional[dict] = None, email: str = JACK, user: str = "u-jack",
                   state: str = "pending") -> str:
        self._n += 1
        aid = f"a{self._n:04d}-0000-4000-8000-000000000000"
        self.actions[aid] = {
            "id": aid, "bugId": bug, "kind": kind, "params": params or {},
            "consents": consents if consents is not None else {
                "cloudSpend": kind == "fix_and_open_pr", "repoWrite": kind in ("fix_and_open_pr", "open_pr"),
            },
            "requestedBy": {"userId": user, "email": email},
            "requestedAt": f"2026-10-01T06:{self._n // 60:02d}:{self._n % 60:02d}.000000+00:00",
            "state": state, "claimedAt": None, "message": None, "steps": None,
        }
        return aid

    def once(self, predicate: Callable[[httpx.Request], bool],
             respond: Callable[[httpx.Request], httpx.Response]) -> None:
        self.rules.append((predicate, respond))

    def posts(self, aid: str) -> list[dict[str, Any]]:
        return [r["body"] for r in self.requests if r["method"] == "POST" and r["path"].endswith(aid)]

    def puts(self, bug: str) -> list[dict[str, Any]]:
        return [r["body"] for r in self.requests if r["method"] == "PUT" and r["path"].endswith(bug)]

    async def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path.removeprefix("/api/bugalizer")
        self.requests.append({
            "method": request.method, "path": path, "body": body,
            "cursor": request.url.params.get("cursor"), "auth": request.headers.get("authorization"),
        })
        for i, (predicate, respond) in enumerate(self.rules):
            if predicate(request):
                del self.rules[i]
                return respond(request)
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": "unauthorized"})
        if request.method == "GET" and path == "/actions":
            return self._list(request)
        if request.method == "POST" and path.startswith("/actions/"):
            return self._advance(path.split("/")[-1], body or {})
        if request.method == "PUT" and path.startswith("/results/"):
            return self._put(path.split("/")[-1], body)
        return httpx.Response(404, json={"error": "no route"})

    def _list(self, request: httpx.Request) -> httpx.Response:
        limit = int(request.url.params.get("limit", "50"))
        items = sorted(
            (a for a in self.actions.values() if a["state"] in ("pending", "claimed")),
            key=lambda a: (a["requestedAt"], a["id"]),
        )
        cursor = request.url.params.get("cursor")
        if cursor is not None:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
            requested, aid = raw.split("|", 1)
            items = [a for a in items if (a["requestedAt"], a["id"]) > (requested, aid)]
        page = items[:limit]
        nxt = self.encode(page[-1]["requestedAt"], page[-1]["id"]) if len(page) == limit else None
        shown = [{k: a[k] for k in ("id", "bugId", "kind", "params", "consents", "requestedBy",
                                     "requestedAt", "state", "claimedAt")} for a in page]
        return httpx.Response(200, json={"actions": shown, "next_cursor": nxt})

    def _advance(self, aid: str, body: dict[str, Any]) -> httpx.Response:
        action = self.actions.get(aid)
        if action is None:
            return httpx.Response(404, json={"error": "Action not found"})
        state = body.get("state")
        if state not in ("claimed", "done", "failed", "refused"):
            return httpx.Response(400, json={"error": "state"})
        if body.get("message") is not None and len(body["message"]) > 2000:
            return httpx.Response(400, json={"error": "message"})
        steps = body.get("steps")
        compound_terminal = action["kind"] == "fix_and_open_pr" and state != "claimed"
        if compound_terminal:
            if (not isinstance(steps, list) or len(steps) != 2
                    or [s.get("step") for s in steps] != ["fix", "open_pr"]):
                return httpx.Response(400, json={"error": "steps"})
            if LEGAL_STEPS.get((steps[0].get("state"), steps[1].get("state"))) != state:
                return httpx.Response(400, json={"error": "step combination"})
        elif steps is not None:
            return httpx.Response(400, json={"error": "steps not allowed"})
        current = action["state"]
        if current == state:
            return httpx.Response(200, json={"changed": False, "action": dict(action)})
        allowed = {("pending", "claimed"), ("pending", "refused"), ("claimed", "done"),
                   ("claimed", "failed"), ("claimed", "refused")}
        if (current, state) not in allowed:
            return httpx.Response(409, json={"error": "transition", "currentState": current})
        action["state"] = state
        if state != "claimed":
            action["message"], action["steps"] = body.get("message"), steps
        if aid in self.drop_after_commit or (state != "claimed" and aid in self.drop_terminal):
            self.drop_after_commit.discard(aid)
            self.drop_terminal.discard(aid)
            raise httpx.ReadError("response lost")
        return httpx.Response(200, json={"changed": True, "action": dict(action)})

    def _put(self, bug: str, body: Any) -> httpx.Response:
        if bug in self.drop_put_once:
            self.drop_put_once.discard(bug)
            raise httpx.ConnectError("lost")
        if bug not in self.bugs:
            return httpx.Response(404, json={"error": "Bug report not found"})
        error = _schema_error(body)
        if error:
            return httpx.Response(400, json={"error": error})
        stored = self.results.get(bug)
        if stored and body["revision"] <= stored["revision"]:
            return httpx.Response(200, json={"applied": False, "storedRevision": stored["revision"]})
        self.results[bug] = body
        return httpx.Response(200, json={"applied": True, "revision": body["revision"]})


class FakeStages:
    """Stand-ins for run_local_analysis / propose_fix / open_pull_request that
    write rows tagged with trigger_ref, like the real stages."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.local = LocalOutcome.COMPLETED
        self.fix = "proposed"
        self.gate: Optional[asyncio.Event] = None
        self.pr: list[Any] = []

    async def run_local_analysis(self, report_id, *, triage_llm=None, localize_llm=None, trigger_ref=None):
        self.calls.append(("local", report_id, triage_llm, localize_llm, trigger_ref))
        if self.gate is not None:
            await self.gate.wait()
        if self.local in (LocalOutcome.COMPLETED, LocalOutcome.FAILED):
            db.analysis_create(
                report_id, "localization",
                "completed" if self.local is LocalOutcome.COMPLETED else "failed",
                result={"pass1": {"candidate_files": []}, "repo_sha": "deadbeef"},
                trigger_ref=trigger_ref,
            )
        return self.local

    async def propose_fix(self, report_id, llm_override=None, *, attribution_ref=None, trigger_ref=None):
        self.calls.append(("fix", report_id, llm_override, attribution_ref, trigger_ref))
        if self.gate is not None:
            await self.gate.wait()
        if self.fix == "proposed":
            p = db.fix_proposal_create(
                bug_report_id=report_id, analysis_id=None, root_cause="rc", explanation="ex",
                diff="--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n", confidence=0.7,
                files_changed=["x"], trigger_ref=trigger_ref,
            )
            db.report_update_status(report_id, "fix_proposed")
            return FixOutcome("proposed", proposal_id=p["id"])
        if self.fix == "failed":
            db.analysis_create(report_id, "fix", "failed", result={"error": "boom"}, trigger_ref=trigger_ref)
            return FixOutcome("failed", error="boom")
        return FixOutcome(self.fix, error="precondition")

    async def open_pull_request(self, report_id, proposal_id):
        self.calls.append(("pr", report_id, proposal_id))
        nxt = self.pr.pop(0) if self.pr else None
        if isinstance(nxt, Exception):
            raise nxt
        return 201, nxt or {"pr_url": "https://github.com/o/r/pull/7", "pr_number": 7,
                            "branch": f"fix/bugalizer-{report_id}", "fix_proposal_id": proposal_id,
                            "created": True}

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    db.reset_conn()
    settings.db_path = ":memory:"
    settings.queue_enabled = False
    db.init_db()
    for name, value in {
        "triage_sync_enabled": False, "triage_max_concurrent": 1, "triage_push_per_tick": 20,
        "triage_action_timeout_minutes": 45.0, "sonicgrid_cloud_users": JACK,
        "fix_provider": "ollama", "default_fix_model": "qwen2.5-coder:14b", "api_keys": "",
    }.items():
        monkeypatch.setattr(settings, name, value)
    ts.reset_runtime_state()
    yield
    ts.reset_runtime_state()
    db.reset_conn()
    settings.db_path = ":memory:"


@pytest.fixture
def fake(monkeypatch) -> FakeTriage:
    sg = FakeTriage()
    monkeypatch.setattr(ts, "http_transport", httpx.MockTransport(sg.handler))
    monkeypatch.setenv("SONICGRID_TRIAGE_TOKEN", TOKEN)
    return sg


@pytest.fixture
def stages(monkeypatch) -> FakeStages:
    st = FakeStages()
    monkeypatch.setattr(ex, "run_local_analysis", st.run_local_analysis)
    monkeypatch.setattr(ex, "propose_fix", st.propose_fix)
    monkeypatch.setattr(ex, "open_pull_request", st.open_pull_request)
    return st


def make_project(config: Optional[dict] = None, **fields: Any) -> str:
    pid = db.project_create(
        name="sonicgrid", repo_url="https://github.com/spherop/sonicgrid",
        ingest_source="supabase", ingest_config=dict(config or CONFIG),
    )["id"]
    db.project_update(pid, head_sha="deadbeef", **fields)
    return pid


def make_report(pid: str, ext: str, fake: Optional[FakeTriage] = None, *,
                status: str = "triaged", localized: bool = True) -> dict[str, Any]:
    r = db.report_create(pid, "Play button", "Play does nothing", "Greg", severity="high")
    db.report_update_fields(r["id"], external_id=ext, ingest_source="supabase")
    db.report_update_status(r["id"], status)
    if localized:
        db.analysis_create(r["id"], "localization", "completed",
                           result={"pass1": {"candidate_files": [{"path": "a.ts", "relevance": 0.9,
                                                                  "reason": "here"}]},
                                   "pass2": {"root_cause_hypothesis": "h"}, "repo_sha": "deadbeef"})
    if fake is not None:
        fake.bugs.add(ext)
    return db.report_get(r["id"])


def bug(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


async def settle(pid: str, ticks: int = 2) -> None:
    for _ in range(ticks):
        await ts.sync_project(pid)
        await ts.wait_idle()
    await ts.sync_project(pid)


def ledger(aid: str) -> dict[str, Any]:
    return db.triage_action_get(aid)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Results push
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_push_once_then_not_again(fake, stages):
    pid = make_project()
    make_report(pid, bug(1), fake)
    await ts.sync_project(pid)
    await ts.sync_project(pid)
    assert len(fake.puts(bug(1))) == 1
    stored = fake.results[bug(1)]
    assert stored["public"]["severity"] == "high"
    assert stored["admin"]["localized"] is True
    assert stored["admin"]["candidateFiles"][0]["path"] == "a.ts"


@pytest.mark.asyncio
async def test_new_analysis_row_pushes_a_higher_revision(fake, stages):
    pid = make_project()
    r = make_report(pid, bug(1), fake)
    await ts.sync_project(pid)
    first = fake.results[bug(1)]["revision"]
    before = db.report_get(r["id"])["updated_at"]
    db.analysis_create(r["id"], "triage", "completed", result={"summary": "s", "confidence": 0.8,
                                                               "category": "ui"})
    assert db.report_get(r["id"])["updated_at"] == before  # the row does not touch updated_at
    await ts.sync_project(pid)
    assert fake.results[bug(1)]["revision"] > first
    assert fake.results[bug(1)]["public"]["summary"] == "s"


@pytest.mark.asyncio
async def test_identical_output_with_newer_timestamp_still_pushes(fake, stages):
    pid = make_project()
    r = make_report(pid, bug(1), fake)
    db.analysis_create(r["id"], "triage", "completed", result={"summary": "same"})
    await ts.sync_project(pid)
    first = fake.results[bug(1)]
    db.analysis_create(r["id"], "triage", "completed", result={"summary": "same"})
    await ts.sync_project(pid)
    second = fake.results[bug(1)]
    assert second["public"] == first["public"] and second["admin"] == first["admin"]
    assert second["bugalizerUpdatedAt"] > first["bugalizerUpdatedAt"]
    assert second["revision"] > first["revision"]


@pytest.mark.asyncio
async def test_interrupted_push_resends_the_same_revision_and_payload(fake, stages):
    pid = make_project()
    make_report(pid, bug(1), fake)
    fake.drop_put_once.add(bug(1))
    out = await ts.sync_project(pid)
    assert out.error == ts.NETWORK_ERROR
    await ts.sync_project(pid)
    sent = fake.puts(bug(1))
    assert len(sent) == 2 and sent[0] == sent[1]
    assert fake.results[bug(1)]["revision"] == sent[0]["revision"]


@pytest.mark.asyncio
async def test_applied_false_ends_the_retry_and_lifts_the_next_revision(fake, stages):
    pid = make_project()
    r = make_report(pid, bug(1), fake)
    fake.results[bug(1)] = {"revision": 2**52}  # a lost ledger: sonicgrid is ahead
    await ts.sync_project(pid)
    await ts.sync_project(pid)
    assert len(fake.puts(bug(1))) == 1
    db.analysis_create(r["id"], "triage", "completed", result={"summary": "new"})
    await ts.sync_project(pid)
    assert fake.results[bug(1)]["revision"] == 2**52 + 1
    assert fake.results[bug(1)]["public"]["summary"] == "new"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 404, 413])
async def test_client_errors_are_not_retried_for_the_same_fingerprint(fake, stages, status):
    pid = make_project()
    r = make_report(pid, bug(1), fake)
    fake.once(lambda q: q.method == "PUT", lambda q: httpx.Response(status, json={"error": "x"}))
    await ts.sync_project(pid)
    await ts.sync_project(pid)
    assert len(fake.puts(bug(1))) == 1
    assert db.triage_result_get(r["id"])["last_error"] == f"http_{status}"
    db.analysis_create(r["id"], "triage", "completed", result={"summary": "changed"})
    await ts.sync_project(pid)
    assert len(fake.puts(bug(1))) == 2  # a new fingerprint is pushed normally


@pytest.mark.asyncio
async def test_a_report_without_external_id_is_never_pushed(fake, stages):
    pid = make_project()
    db.report_create(pid, "Local only", "filed through the API", "jack")
    await ts.sync_project(pid)
    assert not [q for q in fake.requests if q["method"] == "PUT"]


def test_payload_respects_contract_limits():
    report = {"status": "fix_proposed", "severity": "urgent", "analysis_mode": "auto",
              "updated_at": "2026-10-01T00:00:00+00:00"}
    analyses = [
        {"phase": "triage", "status": "completed", "created_at": "2026-10-01T00:00:01+00:00",
         "result": {"summary": "x" * 5000, "category": "c" * 80, "confidence": 1.7}},
        {"phase": "localization", "status": "completed", "created_at": "2026-10-01T00:00:02+00:00",
         "result": {"pass1": {"candidate_files": [{"path": f"f{i}.ts", "relevance": 2,
                                                   "reason": None} for i in range(80)] + ["bare.ts"]}}},
    ]
    proposals = [{"diff": "+" * (DIFF_MAX_BYTES + 1), "confidence": 0.5, "root_cause": "rc",
                  "explanation": "ex", "updated_at": "2026-10-01T00:00:03+00:00",
                  "pr_url": "https://github.com/o/r/pull/3", "pr_number": 3,
                  "branch_name": "fix/bugalizer-1"}]
    payload = build_payload(report, analyses, proposals)
    assert _schema_error({"revision": 1, **payload}) is None
    assert payload["public"]["severity"] is None
    assert len(payload["public"]["summary"]) == 2000
    assert payload["admin"]["diff"] is None
    assert payload["admin"]["triageConfidence"] is None
    assert len(payload["admin"]["candidateFiles"]) == 50
    assert payload["admin"]["pr"] == {"number": 3, "branch": "fix/bugalizer-1"}
    assert payload["bugalizerUpdatedAt"] == "2026-10-01T00:00:03+00:00"
    assert fingerprint(payload) == fingerprint({"revision": 99, **payload})


@pytest.mark.asyncio
async def test_push_budget_spreads_a_backlog_over_ticks(fake, stages, monkeypatch):
    monkeypatch.setattr(settings, "triage_push_per_tick", 2)
    pid = make_project()
    for n in range(5):
        make_report(pid, bug(n), fake, localized=False)
    await ts.sync_project(pid)
    assert len(fake.results) == 2
    await ts.sync_project(pid)
    await ts.sync_project(pid)
    assert len(fake.results) == 5


# ---------------------------------------------------------------------------
# Walk
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_walk_follows_next_cursor_and_never_stores_it(fake, stages, monkeypatch):
    monkeypatch.setattr(ts, "PAGE_LIMIT", 2)
    pid = make_project()
    for n in range(5):
        fake.add_action("analyze_local", bug(900 + n))  # never imported: left pending
    out = await ts.sync_project(pid)
    gets = [q for q in fake.requests if q["method"] == "GET"]
    assert out.actions_seen == 5 and out.unresolved == 5
    assert gets[0]["cursor"] is None and all(q["cursor"] for q in gets[1:])
    fake.requests.clear()
    await ts.sync_project(pid)
    assert [q for q in fake.requests if q["method"] == "GET"][0]["cursor"] is None


@pytest.mark.asyncio
async def test_a_pending_action_behind_claimed_ones_is_reached_in_the_same_tick(fake, stages, monkeypatch):
    monkeypatch.setattr(ts, "PAGE_LIMIT", 2)
    pid = make_project()
    for n in range(3):
        fake.add_action("analyze_local", bug(900 + n), state="claimed")
    r = make_report(pid, bug(1), fake)
    aid = fake.add_action("set_mode", bug(1), params={"mode": "hold"})
    await ts.sync_project(pid)
    assert fake.actions[aid]["state"] == "done"
    assert db.report_get(r["id"])["analysis_mode"] == "hold"


# ---------------------------------------------------------------------------
# Each kind
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_analyze_local_runs_on_its_pinned_local_models(fake, stages):
    pid = make_project(llm_provider="ollama", llm_model="gemma4:12b")
    r = make_report(pid, bug(1), fake)
    aid = fake.add_action("analyze_local", bug(1), email=DAN, user="u-dan")
    await settle(pid)
    assert fake.actions[aid]["state"] == "done"
    kind, rid, triage_llm, localize_llm, trigger = stages.calls[0]
    assert (kind, rid, trigger) == ("local", r["id"], aid)
    assert triage_llm == ("ollama", "gemma4:12b") == localize_llm
    assert ledger(aid)["terminal_acked"] == 1


@pytest.mark.asyncio
async def test_analyze_cloud_for_an_allowlisted_user_runs_and_is_attributed(fake, stages):
    pid = make_project()
    r = make_report(pid, bug(1), fake)
    aid = fake.add_action("analyze_cloud", bug(1))
    await settle(pid)
    assert fake.actions[aid]["state"] == "done"
    kind, rid, override, attribution, trigger = stages.calls[0]
    assert (kind, rid) == ("fix", r["id"])
    assert (override.provider, override.model) == ("ollama", "qwen2.5-coder:14b")
    assert override.api_key is None
    assert attribution == "sonicgrid:u-jack" and trigger == aid
    assert fake.results[bug(1)]["admin"]["rootCause"] == "rc"  # the push carries the outcome


@pytest.mark.asyncio
async def test_analyze_cloud_from_a_non_allowlisted_user_is_refused_with_the_reason(fake, stages):
    pid = make_project()
    make_report(pid, bug(1), fake)
    aid = fake.add_action("analyze_cloud", bug(1), email=DAN, user="u-dan")
    local = fake.add_action("analyze_local", bug(1), email=DAN, user="u-dan")
    await settle(pid, ticks=3)
    assert fake.actions[aid]["state"] == "refused"
    assert "allowlisted" in fake.actions[aid]["message"]
    assert fake.actions[local]["state"] == "done"
    assert stages.kinds() == ["local"]


@pytest.mark.asyncio
@pytest.mark.parametrize("email", [JACK, DAN])
async def test_analyze_local_on_a_paid_local_provider_is_refused_for_everyone(fake, stages, email):
    pid = make_project(llm_provider="anthropic", llm_model="claude-x")
    make_report(pid, bug(1), fake)
    aid = fake.add_action("analyze_local", bug(1), email=email)
    await settle(pid)
    assert fake.actions[aid]["state"] == "refused"
    assert "local models" in fake.actions[aid]["message"]
    assert stages.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fix, pr, state, steps", [
    ("proposed", [], "done", ("done", "done")),
    ("proposed", [OpenPrError(502, "github_error", "x")] * 3, "failed", ("done", "failed")),
    ("proposed", [OpenPrError(409, "diff_does_not_apply", "nope")], "refused", ("done", "refused")),
    ("failed", [], "failed", ("failed", "skipped")),
    ("deferred", [], "refused", ("refused", "skipped")),
])
async def test_fix_and_open_pr_step_combinations(fake, stages, fix, pr, state, steps):
    stages.fix, stages.pr = fix, list(pr)
    pid = make_project()
    make_report(pid, bug(1), fake)
    aid = fake.add_action("fix_and_open_pr", bug(1))
    await settle(pid, ticks=4)
    action = fake.actions[aid]
    assert action["state"] == state
    assert tuple(s["state"] for s in action["steps"]) == steps
    prs = [c for c in stages.calls if c[0] == "pr"]
    if fix == "proposed":
        proposal = db.fix_proposals_by_trigger(aid)[0]["id"]
        assert action["steps"][0]["ref"] == proposal
        assert all(c[2] == proposal for c in prs)
    else:
        assert prs == []


@pytest.mark.asyncio
@pytest.mark.parametrize("provider, state", [("ollama", "done"), ("anthropic", "refused")])
async def test_fix_and_open_pr_from_a_non_allowlisted_user_depends_on_the_provider(
        fake, stages, monkeypatch, provider, state):
    monkeypatch.setattr(settings, "fix_provider", provider)
    pid = make_project()
    make_report(pid, bug(1), fake)
    aid = fake.add_action("fix_and_open_pr", bug(1), email=DAN, user="u-dan")
    await settle(pid, ticks=3)
    assert fake.actions[aid]["state"] == state
    if state == "refused":
        assert stages.calls == []
        assert [s["state"] for s in fake.actions[aid]["steps"]] == ["refused", "skipped"]


@pytest.mark.asyncio
async def test_already_proposed_refuses_the_fix_step(fake, stages):
    stages.fix = "already_proposed"
    pid = make_project()
    make_report(pid, bug(1), fake)
    aid = fake.add_action("fix_and_open_pr", bug(1))
    await settle(pid)
    assert fake.actions[aid]["state"] == "refused"
    assert "request Open PR" in fake.actions[aid]["steps"][0]["message"]
    assert "pr" not in stages.kinds()


@pytest.mark.asyncio
async def test_open_pr_in_progress_stays_claimed_then_finishes(fake, stages):
    stages.pr = [OpenPrError(409, "in_progress", "running")]
    pid = make_project()
    make_report(pid, bug(1), fake, status="fix_proposed")
    aid = fake.add_action("open_pr", bug(1))
    await ts.sync_project(pid)
    assert fake.actions[aid]["state"] == "claimed"
    await ts.sync_project(pid)
    assert fake.actions[aid]["state"] == "done"
    assert "https://github.com/o/r/pull/7" in fake.actions[aid]["message"]


@pytest.mark.asyncio
async def test_open_pr_refusals_carry_the_existing_pr_url(fake, stages):
    stages.pr = [OpenPrError(409, "pr_exists", "already has one", pr_url="https://github.com/o/r/pull/2")]
    pid = make_project()
    make_report(pid, bug(1), fake, status="fix_proposed")
    aid = fake.add_action("open_pr", bug(1))
    await ts.sync_project(pid)
    assert fake.actions[aid]["state"] == "refused"
    assert "https://github.com/o/r/pull/2" in fake.actions[aid]["message"]


@pytest.mark.asyncio
async def test_set_mode_and_close(fake, stages):
    pid = make_project()
    r = make_report(pid, bug(1), fake)
    bad = fake.add_action("set_mode", bug(1), params={"mode": "sometimes"})
    mode = fake.add_action("set_mode", bug(1), params={"mode": "local_only"})
    close = fake.add_action("close", bug(1))
    await settle(pid, ticks=3)
    assert fake.actions[bad]["state"] == "refused"
    assert fake.actions[mode]["state"] == "done"
    assert fake.actions[close]["state"] == "done"
    assert db.report_get(r["id"])["status"] == "closed"
    again = fake.add_action("close", bug(1))
    await ts.sync_project(pid)
    assert fake.actions[again]["state"] == "refused"  # terminal status


@pytest.mark.asyncio
async def test_missing_repo_write_consent_refuses_without_running(fake, stages):
    pid = make_project()
    make_report(pid, bug(1), fake, status="fix_proposed")
    aid = fake.add_action("open_pr", bug(1), consents={"cloudSpend": False, "repoWrite": False})
    await ts.sync_project(pid)
    assert fake.actions[aid]["state"] == "refused"
    assert stages.calls == []


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

def _seed_intent(pid: str, report_id: str, aid: str, kind: str, *, pinned: dict,
                 no_auto_retry: bool, phase: str = "intent") -> None:
    db.triage_action_reserve(aid, pid, report_id, kind, {}, "u-jack")
    db.triage_action_update(aid, expected_phase="reserved", phase=phase,
                            intent_at=ts._now_iso(), pinned_llm=pinned,
                            no_auto_retry=int(no_auto_retry))


@pytest.mark.asyncio
async def test_crash_between_intent_and_dispatch_redispatches_free_work(fake, stages):
    pid = make_project()
    r = make_report(pid, bug(1), fake)
    aid = fake.add_action("analyze_local", bug(1), state="claimed")
    _seed_intent(pid, r["id"], aid, "analyze_local",
                 pinned={"triage": ["ollama", "m"], "localization": ["ollama", "m"]},
                 no_auto_retry=False)
    await settle(pid)
    assert stages.kinds() == ["local"]
    assert stages.calls[0][2] == ("ollama", "m")  # the stored pin, not a fresh resolution
    assert fake.actions[aid]["state"] == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["ollama", "anthropic"])
async def test_crash_between_intent_and_dispatch_never_recharges_analyze_cloud(fake, stages, provider):
    pid = make_project()
    r = make_report(pid, bug(1), fake)
    aid = fake.add_action("analyze_cloud", bug(1), state="claimed")
    _seed_intent(pid, r["id"], aid, "analyze_cloud", pinned={"fix": [provider, "m"]},
                 no_auto_retry=True)
    await settle(pid)
    assert stages.calls == []
    assert fake.actions[aid]["state"] == "failed"
    assert "could not be confirmed" in fake.actions[aid]["message"]


@pytest.mark.asyncio
async def test_untagged_work_from_a_manual_run_is_not_adopted(fake, stages):
    pid = make_project()
    r = make_report(pid, bug(1), fake)
    aid = fake.add_action("analyze_cloud", bug(1), state="claimed")
    _seed_intent(pid, r["id"], aid, "analyze_cloud", pinned={"fix": ["ollama", "m"]},
                 no_auto_retry=True)
    db.fix_proposal_create(bug_report_id=r["id"], analysis_id=None, root_cause="dashboard",
                           explanation="", diff="d", confidence=0.5, files_changed=[])
    await settle(pid)
    assert fake.actions[aid]["state"] == "failed"


@pytest.mark.asyncio
async def test_tagged_evidence_is_adopted_after_a_restart(fake, stages):
    pid = make_project()
    r = make_report(pid, bug(1), fake)
    aid = fake.add_action("analyze_cloud", bug(1), state="claimed")
    _seed_intent(pid, r["id"], aid, "analyze_cloud", pinned={"fix": ["ollama", "m"]},
                 no_auto_retry=True, phase="dispatched")
    db.fix_proposal_create(bug_report_id=r["id"], analysis_id=None, root_cause="rc",
                           explanation="", diff="d", confidence=0.5, files_changed=[],
                           trigger_ref=aid)
    await settle(pid)
    assert stages.calls == []
    assert fake.actions[aid]["state"] == "done"


@pytest.mark.asyncio
async def test_lost_claim_acknowledgement_dispatches_exactly_once(fake, stages):
    pid = make_project()
    make_report(pid, bug(1), fake)
    aid = fake.add_action("analyze_local", bug(1))
    fake.drop_after_commit.add(aid)
    out = await ts.sync_project(pid)
    assert out.error == ts.NETWORK_ERROR and ledger(aid)["phase"] == "reserved"
    ts.reset_runtime_state()  # and a restart, for good measure
    await settle(pid)
    claims = [b for b in fake.posts(aid) if b["state"] == "claimed"]
    assert len(claims) == 2  # the second answered changed:false
    assert stages.kinds() == ["local"]
    assert fake.actions[aid]["state"] == "done"


@pytest.mark.asyncio
async def test_crash_after_fix_done_resumes_only_the_pr_step(fake, stages):
    pid = make_project()
    r = make_report(pid, bug(1), fake, status="fix_proposed")
    aid = fake.add_action("fix_and_open_pr", bug(1), state="claimed")
    _seed_intent(pid, r["id"], aid, "fix_and_open_pr", pinned={"fix": ["ollama", "m"]},
                 no_auto_retry=False, phase="dispatched")
    db.triage_action_update(aid, phase="fix_done", fix_proposal_id="fp_1",
                            steps=[{"step": "fix", "state": "done", "ref": "fp_1"}])
    stages.pr = [OpenPrError(502, "github_error", "after the push")]
    await ts.sync_project(pid)            # PR attempt 1 fails transiently
    ts.reset_runtime_state()              # restart after the branch push
    await settle(pid)
    assert "fix" not in stages.kinds()
    assert [c[2] for c in stages.calls] == ["fp_1", "fp_1"]
    assert fake.actions[aid]["state"] == "done"
    assert [s["state"] for s in fake.actions[aid]["steps"]] == ["done", "done"]


@pytest.mark.asyncio
async def test_timeout_then_late_completion_keeps_the_posted_failure(fake, stages, monkeypatch):
    stages.gate = asyncio.Event()
    pid = make_project()
    make_report(pid, bug(1), fake)
    aid = fake.add_action("fix_and_open_pr", bug(1))
    await ts.sync_project(pid)
    await asyncio.sleep(0)                # let the task start
    assert stages.kinds() == ["fix"] and ts._task_running(aid)
    monkeypatch.setattr(settings, "triage_action_timeout_minutes", 0.0)
    await ts.sync_project(pid)
    assert fake.actions[aid]["state"] == "failed"
    assert "timed out" in fake.actions[aid]["message"]
    assert ts._task_running(aid)          # the slot is held until the task exits
    stages.gate.set()
    await ts.wait_idle()
    await ts.sync_project(pid)
    assert "pr" not in stages.kinds()
    assert ledger(aid)["late_outcome"] == "fix_done"
    assert ledger(aid)["outcome"] == "failed"
    assert aid not in ts._holds


@pytest.mark.asyncio
async def test_terminal_ack_lost_then_drained_from_the_ledger_after_restart(fake, stages):
    pid = make_project()
    make_report(pid, bug(1), fake)
    aid = fake.add_action("set_mode", bug(1), params={"mode": "hold"})
    fake.drop_terminal.add(aid)
    out = await ts.sync_project(pid)      # sonicgrid commits `done`, the answer is lost
    assert out.error == ts.NETWORK_ERROR
    assert fake.actions[aid]["state"] == "done"
    assert ledger(aid)["terminal_acked"] == 0
    ts.reset_runtime_state()
    fake.requests.clear()
    await ts.sync_project(pid)            # the open-only listing no longer shows it
    assert [q for q in fake.requests if q["method"] == "GET"]
    assert fake.posts(aid) == [{"state": "done", "message": "Analysis mode set to hold"}]
    assert ledger(aid)["terminal_acked"] == 1


# ---------------------------------------------------------------------------
# Ownership and concurrency
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_manual_run_during_a_tick_gets_tick_in_progress(fake, stages):
    pid = make_project()
    lock = ts._project_lock(pid)
    async with lock:
        with pytest.raises(ts.TickInProgress):
            await ts.sync_project(pid, manual=True)


@pytest.mark.asyncio
async def test_overlapping_ticks_dispatch_once(fake, stages):
    stages.gate = asyncio.Event()
    pid = make_project()
    make_report(pid, bug(1), fake)
    aid = fake.add_action("analyze_local", bug(1))
    await asyncio.gather(ts.sync_project(pid), ts.sync_project(pid))
    await ts.sync_project(pid)
    stages.gate.set()
    await settle(pid)
    assert stages.kinds() == ["local"]
    assert fake.actions[aid]["state"] == "done"


@pytest.mark.asyncio
async def test_llm_slots_are_bounded_and_extra_actions_stay_pending(fake, stages):
    stages.gate = asyncio.Event()
    pid = make_project()
    make_report(pid, bug(1), fake)
    make_report(pid, bug(2), fake)
    first = fake.add_action("analyze_local", bug(1))
    second = fake.add_action("analyze_local", bug(2))
    mode = fake.add_action("set_mode", bug(1), params={"mode": "hold"})
    await ts.sync_project(pid)
    assert fake.actions[first]["state"] == "claimed"
    assert fake.actions[second]["state"] == "pending"   # no free LLM slot
    assert fake.actions[mode]["state"] == "pending"     # report 1 is busy
    stages.gate.set()
    await settle(pid, ticks=3)
    assert {fake.actions[a]["state"] for a in (first, second, mode)} == {"done"}


def test_a_second_project_on_the_same_triage_source_is_rejected(fake):
    make_project()
    with TestClient(app) as client:
        resp = client.post("/api/v1/projects", json={
            "name": "twin", "repo_url": "https://github.com/x/y",
            "ingest_source": "supabase", "ingest_config": CONFIG,
        })
        assert resp.status_code == 409 and resp.json()["detail"] == "triage_source_in_use"
        other = dict(CONFIG)
        other.pop("triage_credential_env")
        ok = client.post("/api/v1/projects", json={
            "name": "poll-only", "repo_url": "https://github.com/x/y",
            "ingest_source": "supabase", "ingest_config": other,
        })
        assert ok.status_code == 201


@pytest.mark.asyncio
async def test_existing_duplicates_are_skipped(fake, stages):
    a = make_project()
    b = make_project()  # bypasses the API check, as a hand-edited DB would
    for pid in (a, b):
        out = await ts.sync_project(pid)
        assert out.error == ts.DUPLICATE_SOURCE
    assert fake.requests == []


def test_toggling_the_triage_credential_keeps_the_poll_checkpoint():
    pid = make_project()
    gen = db.project_get(pid)["ingest_generation"]
    config = dict(CONFIG)
    config.pop("triage_credential_env")
    db.project_update(pid, ingest_config=config)
    assert db.project_get(pid)["ingest_generation"] == gen


# ---------------------------------------------------------------------------
# Secrecy and operator surface
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_token_and_requester_emails_never_stored_logged_or_returned(fake, stages, caplog):
    caplog.set_level(logging.DEBUG)
    pid = make_project()
    make_report(pid, bug(1), fake)
    fake.add_action("analyze_cloud", bug(1), email=DAN, user="u-dan")
    fake.add_action("analyze_local", bug(1), email=JACK)
    await settle(pid, ticks=3)
    fake.once(lambda q: True, lambda q: httpx.Response(401))
    await ts.sync_project(pid)
    dump = "\n".join(db._get_conn().iterdump())
    for secret in (TOKEN, DAN, JACK):
        assert secret not in caplog.text
        assert secret not in dump
    for q in fake.requests:
        for secret in (DAN, JACK):
            assert secret not in json.dumps(q["body"] or {})


def test_status_run_and_health_endpoints(fake, stages):
    pid = make_project()
    make_report(pid, bug(1), fake)
    with TestClient(app) as client:
        run = client.post(f"/api/v1/projects/{pid}/triage-sync/run")
        assert run.status_code == 200 and run.json()["results_pushed"] == 1
        status = client.get(f"/api/v1/projects/{pid}/triage-sync").json()
        assert status["configured"] and status["credential_present"]
        assert status["results_tracked"] == 1 and status["results_pending"] == 0
        health = client.get("/health").json()
        assert health["triage_sync"] == {"enabled": False, "projects": 1, "failing": 0}
        for body in (run.text, json.dumps(status), json.dumps(health)):
            assert TOKEN not in body
        bare = db.project_create(name="bare", repo_url="https://github.com/x/z")["id"]
        assert client.post(f"/api/v1/projects/{bare}/triage-sync/run").status_code == 404


# ---------------------------------------------------------------------------
# The real stages' new parameters (pins, attribution, tags)
# ---------------------------------------------------------------------------

def _fix_ready_report(tmp_path) -> dict[str, Any]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def divide(a, b):\n    return a / b\n", encoding="utf-8")
    pid = db.project_create(name="demo", repo_url="https://example.com/r.git")["id"]
    db.project_update(pid, repo_path=str(repo), head_sha="deadbeef", fix_llm_provider="anthropic")
    r = db.report_create(pid, "Zero division", "divide(1,0) crashes", "qa")
    db.report_update_status(r["id"], "triaged")
    db.analysis_create(r["id"], "localization", "completed", result={
        "pass1": {"candidate_files": [{"path": "app.py", "relevance": 0.9, "reason": "r"}],
                  "confidence": 0.9},
        "pass2": {"root_cause_hypothesis": "no guard"}, "repo_sha": "deadbeef"})
    return r


_PROPOSAL = {
    "root_cause": "no guard", "explanation": "guard b",
    "diff": "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,3 @@\n def divide(a, b):\n+    assert b\n     return a / b\n",
    "confidence": 0.8, "files_changed": ["app.py"],
}


@pytest.mark.asyncio
async def test_real_propose_fix_uses_the_pin_and_records_requester_attribution(tmp_path):
    r = _fix_ready_report(tmp_path)  # the project now says anthropic for Stage 4
    mock = AsyncMock(return_value=LLMResponse(content=json.dumps(_PROPOSAL), prompt_tokens=10,
                                              completion_tokens=5, model="qwen", provider="ollama"))
    with patch("bugalizer.pipeline.fix_proposer.llm_client.complete", new=mock):
        from bugalizer.models import LLMOverride
        out = await propose_fix(r["id"], llm_override=LLMOverride(provider="ollama", model="pinned"),
                                attribution_ref="sonicgrid:u-jack", trigger_ref="act-1")
    assert out.kind == "proposed"
    assert mock.await_args.kwargs["provider"] == "ollama"
    assert mock.await_args.kwargs["model"] == "pinned"
    assert mock.await_args.kwargs["api_key"] is None
    assert db.fix_proposals_by_trigger("act-1")[0]["id"] == out.proposal_id
    assert [a["phase"] for a in db.analyses_by_trigger("act-1")] == ["fix"]
    usage = db.token_usage_summary()["attribution"]
    assert {"key_source": "env", "key_ref": "sonicgrid:u-jack"}.items() <= usage[0].items()


@pytest.mark.asyncio
async def test_real_propose_fix_outcomes_for_untagged_callers_are_unchanged(tmp_path):
    r = _fix_ready_report(tmp_path)
    mock = AsyncMock(return_value=LLMResponse(content=json.dumps(_PROPOSAL), prompt_tokens=1,
                                              completion_tokens=1, model="m", provider="anthropic"))
    with patch("bugalizer.pipeline.fix_proposer.llm_client.complete", new=mock):
        first = await propose_fix(r["id"])
        db.report_update_status(r["id"], "triaged")
        second = await propose_fix(r["id"])
    assert first.kind == "proposed" and second.kind == "already_proposed"
    assert second.proposal_id == first.proposal_id
    assert db.token_usage_summary()["attribution"][0]["key_ref"] is None
    assert db.fix_proposals_for_report(r["id"])[0]["trigger_ref"] is None


@pytest.mark.asyncio
async def test_process_triaged_passes_the_pin_and_the_tag():
    pid = db.project_create(name="p", repo_url="https://example.com/r.git", llm_provider="anthropic")["id"]
    r = db.report_create(pid, "t", "d", "qa")
    db.report_update_status(r["id"], "triaged")
    with patch("bugalizer.pipeline.orchestrator.triage_report", new=AsyncMock()) as tri:
        assert await process_triaged(r["id"], llm=("ollama", "gemma"), trigger_ref="act-2") is True
    kwargs = tri.await_args.kwargs
    assert (kwargs["provider"], kwargs["model"], kwargs["trigger_ref"]) == ("ollama", "gemma", "act-2")


@pytest.mark.asyncio
async def test_a_config_change_after_authorization_does_not_change_the_provider(fake, stages):
    stages.gate = asyncio.Event()
    pid = make_project(llm_provider="ollama", llm_model="gemma4:12b")
    make_report(pid, bug(1), fake)
    make_report(pid, bug(2), fake)
    first = fake.add_action("analyze_local", bug(1))
    second = fake.add_action("analyze_local", bug(2))  # waits for the slot
    await ts.sync_project(pid)
    db.project_update(pid, llm_provider="anthropic")   # after the first was authorized
    stages.gate.set()
    await settle(pid, ticks=3)
    assert stages.calls[0][2] == ("ollama", "gemma4:12b")
    assert fake.actions[first]["state"] == "done"
    assert fake.actions[second]["state"] == "refused"  # authorized after the change
