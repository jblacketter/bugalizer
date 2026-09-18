"""Tests for POST /reports/{id}/open-pr (Phase 8 / B2, docs/phases/open-pr.md).

Git runs for real. The "GitHub" remote is a local bare repo served by a stdlib
HTTP server wrapping `git http-backend`; it answers 401 unless the request
carries the expected Authorization header, and it records every request. The
GitHub REST API is an httpx MockTransport that records calls and holds PR
state. The analysis clone is a normal clone of the bare repo whose `origin` is
then pointed at an unreachable SSH URL, so every test proves the module never
uses `origin`.

Every test ends with the harness's teardown checks: the clone is isolated
(HEAD, branches, status, FETCH_HEAD, .git/config, remotes unchanged; no
`refs/bugalizer/*`; no worktree), no git argv carries a force flag or `+`
refspec (except the exact `worktree remove --force` argv), every remote git
call names the canonical URL with the env-only credential, and the sentinel
token appears in no response, log record, argv or git config.
"""

from __future__ import annotations

import asyncio
import base64
import difflib
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from bugalizer import db
from bugalizer.config import settings
from bugalizer.db import init_db
from bugalizer.git_ops import pull_request as pr_mod
from bugalizer.git_ops.pull_request import (
    _parse_numstat_z,
    _strip_level,
    assert_push_allowed,
    parse_github_slug,
    redact,
)
from bugalizer.main import app


def _http_backend() -> Optional[str]:
    try:
        exec_path = subprocess.run(
            ["git", "--exec-path"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    path = Path(exec_path) / "git-http-backend"
    return str(path) if path.exists() else None


HTTP_BACKEND = _http_backend()
pytestmark = pytest.mark.skipif(
    HTTP_BACKEND is None,
    reason="git-http-backend not found (git --exec-path); the open-pr harness needs it",
)

TOKEN = "ghp_SENTINELtoken0123456789abcdef"
TOKEN_B64 = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
OWNER, REPO = "o", "r"
API_BASE = "https://api.github.test"

BASE_SOURCE = "def add(a, b):\n    return a - b\n\n\ndef sub(a, b):\n    return a - b\n"
FIX_A = BASE_SOURCE.replace("def add(a, b):\n    return a - b", "def add(a, b):\n    return a + b")
FIX_B = BASE_SOURCE.replace("def add(a, b):\n    return a - b", "def add(a, b):\n    return b + a")
UPSTREAM = BASE_SOURCE.replace("def add(a, b):\n    return a - b", "def add(a, b):\n    return sum((a, b))")


def make_diff(new: str, *, old: str = BASE_SOURCE, path: str = "app.py", prefixes: bool = True) -> str:
    a, b = (f"a/{path}", f"b/{path}") if prefixes else (path, path)
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True), a, b,
    ))


_TEST_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Tester", "GIT_AUTHOR_EMAIL": "tester@example.com",
    "GIT_COMMITTER_NAME": "Tester", "GIT_COMMITTER_EMAIL": "tester@example.com",
    "GIT_TERMINAL_PROMPT": "0",
}


def git(*args: str, cwd: Path | str, check: bool = True, env: dict | None = None) -> str:
    full_env = {**os.environ, **_TEST_GIT_ENV, **(env or {})}
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=str(cwd), capture_output=True, text=True, env=full_env,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {args} failed: {result.stderr}")
    return result.stdout


# ---------------------------------------------------------------------------
# Fake GitHub: smart-HTTP git server + REST API
# ---------------------------------------------------------------------------

class GitServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, project_root: Path) -> None:
        super().__init__(("127.0.0.1", 0), _GitHandler)
        self.project_root = project_root
        self.expected_auth = f"basic {TOKEN_B64}"
        self.requests: list[dict[str, Any]] = []
        self.reject_push = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _GitHandler(BaseHTTPRequestHandler):
    server: GitServer

    def log_message(self, *args: Any) -> None:  # quiet
        pass

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def _read_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            while True:
                size = int(self.rfile.readline().strip(), 16)
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _reply(self, code: int, body: bytes = b"", headers: dict | None = None) -> None:
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self) -> None:
        path, _, query = self.path.partition("?")
        authorized = self.headers.get("Authorization") == self.server.expected_auth
        self.server.requests.append(
            {"method": self.command, "path": path, "query": query, "authorized": authorized}
        )
        if not authorized:
            self._reply(401, b"auth required\n", {"WWW-Authenticate": 'Basic realm="fake"'})
            return
        body = self._read_body()
        if self.server.reject_push and path.endswith("/git-receive-pack") and self.command == "POST":
            self._reply(500, b"push rejected by test server\n", {"Content-Type": "text/plain"})
            return
        env = {
            "PATH": os.environ.get("PATH", ""),
            "GIT_PROJECT_ROOT": str(self.server.project_root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "REQUEST_METHOD": self.command,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "REMOTE_USER": "bugalizer",
            "REMOTE_ADDR": "127.0.0.1",
            "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
            "HTTP_GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
        }
        if self.headers.get("Content-Encoding"):
            env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
        proc = subprocess.run([HTTP_BACKEND], input=body, env=env, capture_output=True)
        raw = proc.stdout
        sep = b"\r\n\r\n" if b"\r\n\r\n" in raw else b"\n\n"
        head, _, payload = raw.partition(sep)
        code, headers = 200, {}
        for line in head.decode("latin-1").splitlines():
            key, _, value = line.partition(":")
            if key.lower() == "status":
                code = int(value.strip().split()[0])
            elif key and key.lower() != "content-length":
                headers[key] = value.strip()
        self._reply(code, payload, headers)


class FakeGitHub:
    """GitHub REST: list/create pulls and list a PR's commits."""

    def __init__(self, bare: Path) -> None:
        self.bare = bare
        self.prs: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str]] = []
        self.posted: list[dict[str, Any]] = []
        self.fail_post: Optional[tuple[int, dict]] = None
        self.hide_lookup_once = False

    def _tip(self, branch: str) -> Optional[str]:
        out = git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", cwd=self.bare, check=False)
        return out.strip() or None

    def _commits(self, base_sha: str, tip: str) -> list[dict[str, Any]]:
        out = git("log", "--format=%H%x1f%B%x1e", f"{base_sha}..{tip}", cwd=self.bare)
        commits = []
        for record in out.split("\x1e"):
            record = record.strip("\n")
            if not record:
                continue
            sha, _, message = record.partition("\x1f")
            commits.append({"sha": sha.strip(), "commit": {"message": message}})
        return commits

    def add_pr(self, branch: str, *, base: str = "main") -> dict[str, Any]:
        tip = self._tip(branch)
        assert tip, f"branch {branch} missing on the bare repo"
        base_sha = git("merge-base", f"refs/heads/{base}", tip, cwd=self.bare).strip()
        number = len(self.prs) + 1
        pr = {
            "number": number,
            "html_url": f"https://github.com/{OWNER}/{REPO}/pull/{number}",
            "state": "open", "merged": False,
            "head": {"ref": branch, "sha": tip},
            "base": {"ref": base},
            "commits": self._commits(base_sha, tip),
        }
        self.prs.append(pr)
        return pr

    def merge(self, pr: dict[str, Any]) -> None:
        pr["state"], pr["merged"] = "closed", True

    def _public(self, pr: dict[str, Any]) -> dict[str, Any]:
        tip = self._tip(pr["head"]["ref"]) or pr["head"]["sha"]
        return {**{k: v for k, v in pr.items() if k != "commits"},
                "head": {**pr["head"], "sha": tip}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"message": "Bad credentials"})
        if path == f"/repos/{OWNER}/{REPO}/pulls" and request.method == "GET":
            if self.hide_lookup_once:
                self.hide_lookup_once = False
                return httpx.Response(200, json=[])
            owner, _, branch = request.url.params["head"].partition(":")
            assert owner == OWNER
            return httpx.Response(200, json=[self._public(p) for p in self.prs if p["head"]["ref"] == branch])
        if path == f"/repos/{OWNER}/{REPO}/pulls" and request.method == "POST":
            if self.fail_post is not None:
                return httpx.Response(self.fail_post[0], json=self.fail_post[1])
            body = json.loads(request.content)
            self.posted.append(body)
            if not self._tip(body["head"]):
                return httpx.Response(422, json={"message": "Validation Failed"})
            if any(p["head"]["ref"] == body["head"] and p["state"] == "open" for p in self.prs):
                return httpx.Response(422, json={
                    "message": "Validation Failed",
                    "errors": [{"message": f"A pull request already exists for {OWNER}:{body['head']}."}],
                })
            pr = self.add_pr(body["head"], base=body["base"])
            pr.update(title=body["title"], body=body["body"], draft=body.get("draft"))
            return httpx.Response(201, json=self._public(pr))
        match = re.fullmatch(rf"/repos/{OWNER}/{REPO}/pulls/(\d+)/commits", path)
        if match and request.method == "GET":
            pr = next(p for p in self.prs if p["number"] == int(match.group(1)))
            return httpx.Response(200, json=pr["commits"])
        return httpx.Response(404, json={"message": "Not Found"})


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class Harness:
    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        self.tmp = tmp
        self.caplog = caplog
        self.responses: list[str] = []
        self.git_calls: list[dict[str, Any]] = []
        self.skip_isolation_check = False
        # (command, entered, release): the next git call running `command`
        # completes, then holds its worker thread until `release` is set.
        self.block: Optional[tuple[str, threading.Event, threading.Event]] = None
        self.events: list[tuple[str, tuple[str, ...]]] = []

        root = tmp / "remote"
        self.bare = root / OWNER / f"{REPO}.git"
        self.bare.parent.mkdir(parents=True)
        git("init", "--bare", "-q", "-b", "main", str(self.bare), cwd=tmp)
        git("config", "http.receivepack", "true", cwd=self.bare)
        self.seed = tmp / "seed"
        git("clone", "-q", str(self.bare), str(self.seed), cwd=tmp)
        (self.seed / "app.py").write_text(BASE_SOURCE)
        git("add", "app.py", cwd=self.seed)
        git("commit", "-q", "-m", "initial", cwd=self.seed)
        git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=self.seed)

        self.server = GitServer(root)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.github = FakeGitHub(self.bare)

        repos = tmp / "repos"
        monkeypatch.setattr(settings, "repos_dir", str(repos))
        monkeypatch.setattr(settings, "github_token", SecretStr(TOKEN))
        monkeypatch.setattr(settings, "github_web_base", self.server.url)
        monkeypatch.setattr(settings, "github_api_base", API_BASE)
        monkeypatch.setattr(pr_mod, "_http_transport", httpx.MockTransport(self.github.handler))
        monkeypatch.setattr(pr_mod, "_active_claims", {})
        monkeypatch.setattr(pr_mod, "_project_locks", {})
        original = pr_mod._run_git

        def recording_run_git(args, cwd, *, env, input=None):
            self.git_calls.append({"args": list(args), "cwd": cwd, "env": dict(env)})
            self.events.append(("start", tuple(args)))
            try:
                return original(args, cwd, env=env, input=input)
            finally:
                block = self.block
                if block is not None and args[0] == block[0]:
                    self.block = None
                    block[1].set()
                    assert block[2].wait(30), "test never released the blocked git worker"
                self.events.append(("end", tuple(args)))

        monkeypatch.setattr(pr_mod, "_run_git", recording_run_git)
        caplog.set_level(logging.DEBUG)

        project = db.project_create(name="P", repo_url=f"https://github.com/{OWNER}/{REPO}")
        self.project_id = project["id"]
        self.clone = repos / self.project_id
        git("clone", "-q", "--branch", "main", "--single-branch", str(self.bare), str(self.clone), cwd=tmp)
        # origin is unreachable: the module must never use it.
        git("remote", "set-url", "origin", f"git@github.com:{OWNER}/{REPO}.git", cwd=self.clone)
        (self.clone / ".git" / "FETCH_HEAD").write_text("0" * 40 + "\t\tbranch 'main' of somewhere\n")
        db.project_update(self.project_id, repo_path=str(self.clone), head_sha=self.main_tip())
        self.client = TestClient(app)
        self.initial_clone_state = self.clone_state()

    # -- setup helpers ------------------------------------------------------

    def report(self, *diffs: str, status: str = "fix_proposed") -> tuple[str, list[str]]:
        """A report with one proposal per diff (oldest first). Returns ids."""
        report = db.report_create(self.project_id, "Adder subtracts", "add() subtracts", "tester")
        db.analysis_create(report["id"], "fix", "completed",
                           llm_provider="anthropic", llm_model="claude-test")
        proposal_ids = []
        for i, diff in enumerate(diffs or (make_diff(FIX_A),)):
            proposal = db.fix_proposal_create(
                bug_report_id=report["id"], analysis_id=None,
                root_cause=f"root cause {i}: add uses minus",
                explanation=f"explanation {i}", diff=diff, confidence=0.9,
                files_changed=["app.py"],
            )
            proposal_ids.append(proposal["id"])
        db.report_update_status(report["id"], status)
        return report["id"], proposal_ids

    def advance_main(self, source: str = UPSTREAM) -> None:
        git("fetch", "-q", "origin", cwd=self.seed)
        git("checkout", "-q", "-B", "work", "origin/main", cwd=self.seed)
        (self.seed / "app.py").write_text(source)
        git("commit", "-q", "-am", "upstream change", cwd=self.seed)
        git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=self.seed)

    def push_foreign_branch(self, report_id: str, source: str = FIX_A) -> str:
        git("fetch", "-q", "origin", cwd=self.seed)
        git("checkout", "-q", "-B", "hand", "origin/main", cwd=self.seed)
        (self.seed / "app.py").write_text(source)
        git("commit", "-q", "-am", "hand-made fix", cwd=self.seed)
        git("push", "-q", "origin", f"HEAD:refs/heads/fix/bugalizer-{report_id}", cwd=self.seed)
        return git("rev-parse", "HEAD", cwd=self.seed).strip()

    def crash_after_pr(self, report_id: str) -> None:
        """Persisted state of a process that died after the PR POST, before
        record_pull_request: PR fields unset, report still claimed."""
        conn = db._get_conn()
        conn.execute(
            "UPDATE fix_proposals SET pr_url = NULL, pr_number = NULL, pr_opened_at = NULL, "
            "status = 'proposed' WHERE bug_report_id = ?", (report_id,))
        conn.execute(
            "UPDATE bug_reports SET status = 'fix_approved', claim_token = 'dead-token' WHERE id = ?",
            (report_id,))
        conn.commit()
        pr_mod._active_claims.clear()

    def rows(self, report_id: str) -> dict[str, dict[str, Any]]:
        conn = db._get_conn()
        out = {"report": dict(conn.execute("SELECT * FROM bug_reports WHERE id = ?", (report_id,)).fetchone())}
        for row in conn.execute("SELECT * FROM fix_proposals WHERE bug_report_id = ?", (report_id,)):
            out[row["id"]] = dict(row)
        return out

    def restore(self, snapshot: dict[str, dict[str, Any]]) -> None:
        conn = db._get_conn()
        for key, row in snapshot.items():
            table = "bug_reports" if key == "report" else "fix_proposals"
            cols = [c for c in row if c != "id"]
            conn.execute(
                f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = ?",
                [row[c] for c in cols] + [row["id"]],
            )
        conn.commit()

    # -- actions ------------------------------------------------------------

    def open_pr(self, report_id: str, body: Optional[dict] = None) -> httpx.Response:
        kwargs = {"json": body} if body is not None else {}
        resp = self.client.post(f"/api/v1/reports/{report_id}/open-pr", **kwargs)
        self.responses.append(resp.text)
        return resp

    # -- observations -------------------------------------------------------

    def main_tip(self) -> str:
        return git("rev-parse", "refs/heads/main", cwd=self.bare).strip()

    def bare_ref(self, name: str) -> Optional[str]:
        return git("rev-parse", "--verify", "--quiet", name, cwd=self.bare, check=False).strip() or None

    def bare_refs(self) -> str:
        return git("for-each-ref", cwd=self.bare)

    @property
    def pushes(self) -> int:
        return sum(1 for r in self.server.requests
                   if r["method"] == "POST" and r["path"].endswith("/git-receive-pack"))

    @property
    def pr_posts(self) -> int:
        return sum(1 for m, p in self.github.calls if m == "POST")

    def git_args(self, command: str) -> list[list[str]]:
        return [c["args"] for c in self.git_calls if c["args"] and c["args"][0] == command]

    def clone_state(self) -> dict[str, Any]:
        return {
            "head": git("rev-parse", "HEAD", cwd=self.clone),
            "branches": git("branch", "--list", "--all", cwd=self.clone),
            "status": git("status", "--porcelain", cwd=self.clone),
            "fetch_head": (self.clone / ".git" / "FETCH_HEAD").read_bytes(),
            "config": (self.clone / ".git" / "config").read_bytes(),
            "remotes": git("remote", "-v", cwd=self.clone),
        }

    # -- teardown checks ----------------------------------------------------

    def check_invariants(self) -> None:
        if not self.skip_isolation_check:
            assert self.clone_state() == self.initial_clone_state
        assert git("for-each-ref", "refs/bugalizer/", cwd=self.clone) == ""
        worktrees = [l for l in git("worktree", "list", "--porcelain", cwd=self.clone).splitlines()
                     if l.startswith("worktree ")]
        assert len(worktrees) == 1, worktrees
        wt_root = Path(settings.repos_dir).resolve() / ".worktrees"
        assert not wt_root.exists() or not any(p.is_dir() for p in wt_root.glob("*/*"))

        canonical = f"{self.server.url}/{OWNER}/{REPO}.git"
        for call in self.git_calls:
            args = call["args"]
            assert "origin" not in args, args
            if "--force" in args:
                assert args[:3] == ["worktree", "remove", "--force"] and len(args) == 4, args
            if args[0] in ("fetch", "push", "update-ref"):
                assert not any(a in ("-f", "--force") or a.startswith("--force-with-lease")
                               or a.startswith("+") for a in args), args
            if args[0] == "fetch":
                assert "--no-write-fetch-head" in args, args
            if args[0] in ("fetch", "push", "ls-remote"):
                assert canonical in args, args
                env = call["env"]
                assert env["GIT_CONFIG_KEY_0"] == f"http.{self.server.url}/.extraheader"
                assert env["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {TOKEN_B64}"
                assert env["GIT_CONFIG_KEY_1"] == "credential.helper"
                assert env["GIT_CONFIG_VALUE_1"] == ""
                assert env["GIT_TERMINAL_PROMPT"] == "0"
            else:
                assert "GIT_CONFIG_VALUE_0" not in call["env"], args
            joined = " ".join(args)
            assert TOKEN not in joined and TOKEN_B64 not in joined

        assert all(r["authorized"] for r in self.server.requests)
        secrets = (TOKEN, TOKEN_B64)
        for text in [*self.responses, self.caplog.text]:
            for secret in secrets:
                assert secret not in text
        for path in (self.clone / ".git").rglob("config*"):
            data = path.read_bytes()
            for secret in secrets:
                assert secret.encode() not in data

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(autouse=True)
def fresh_db():
    db.reset_conn()
    settings.db_path = ":memory:"
    settings.queue_enabled = False
    init_db()
    yield


@pytest.fixture
def h(tmp_path, monkeypatch, caplog):
    harness = Harness(tmp_path, monkeypatch, caplog)
    yield harness
    try:
        harness.check_invariants()
    finally:
        harness.close()


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("prefixes", [True, False], ids=["p1", "p0"])
def test_happy_path_opens_pr(h, prefixes):
    report_id, (proposal_id,) = h.report(make_diff(FIX_A, prefixes=prefixes))
    main_before = h.main_tip()

    resp = h.open_pr(report_id)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    branch = f"fix/bugalizer-{report_id}"
    assert body == {"pr_url": f"https://github.com/{OWNER}/{REPO}/pull/1", "pr_number": 1,
                    "branch": branch, "fix_proposal_id": proposal_id, "created": True}
    # The branch is exactly the diff on top of the default-branch tip.
    tip = h.bare_ref(f"refs/heads/{branch}")
    assert git("rev-parse", f"{tip}^", cwd=h.bare).strip() == main_before
    assert git("show", f"{tip}:app.py", cwd=h.bare) == FIX_A
    assert git("diff", "--name-only", main_before, tip, cwd=h.bare).split() == ["app.py"]
    message = git("log", "-1", "--format=%B%n%an <%ae>", tip, cwd=h.bare)
    assert f"Bugalizer-Report: {report_id}" in message
    assert f"Bugalizer-Proposal: {proposal_id}" in message
    assert "Bugalizer <bugalizer@localhost>" in message
    assert h.bare_ref("refs/heads/main") == main_before
    # The PR request.
    (posted,) = h.github.posted
    assert posted["base"] == "main" and posted["head"] == branch and posted["draft"] is False
    assert posted["title"] == f"fix: Adder subtracts (bugalizer {report_id})"
    assert "root cause 0: add uses minus" in posted["body"]
    assert "explanation 0" in posted["body"] and "0.90" in posted["body"]
    assert "cloud tier, `anthropic/claude-test`" in posted["body"]
    assert posted["body"].rstrip().endswith(pr_mod.PR_BODY_FOOTER)
    # Persisted state.
    rows = h.rows(report_id)
    assert rows["report"]["status"] == "fix_committed"
    assert rows["report"]["claim_token"] is None
    proposal = rows[proposal_id]
    assert proposal["status"] == "pr_opened" and proposal["pr_number"] == 1
    assert proposal["pushed_sha"] == tip and proposal["branch_name"] == branch
    assert proposal["pr_opened_at"]
    assert h.pushes == 1 and h.pr_posts == 1


# ---------------------------------------------------------------------------
# 2. Idempotency and ownership
# ---------------------------------------------------------------------------

def test_repeat_call_returns_same_pr_without_writes(h):
    report_id, (proposal_id,) = h.report()
    first = h.open_pr(report_id).json()
    calls_before = len(h.github.calls)

    resp = h.open_pr(report_id)

    assert resp.status_code == 200
    assert resp.json() == {**first, "created": False}
    assert h.pushes == 1 and h.pr_posts == 1
    assert len(h.github.calls) == calls_before  # answered from the DB


def test_ownership_is_report_wide(h):
    report_id, (older, newer) = h.report(make_diff(FIX_A), make_diff(FIX_B))

    opened = h.open_pr(report_id, {"fix_proposal_id": older})
    assert opened.status_code == 201 and opened.json()["fix_proposal_id"] == older

    bodyless = h.open_pr(report_id)
    assert bodyless.status_code == 200
    assert bodyless.json()["fix_proposal_id"] == older

    other = h.open_pr(report_id, {"fix_proposal_id": newer})
    assert other.status_code == 409
    assert other.json()["code"] == "pr_exists"
    assert other.json()["fix_proposal_id"] == older
    assert other.json()["pr_number"] == 1
    assert h.pushes == 1 and h.pr_posts == 1


def test_foreign_and_unknown_proposal_ids_are_not_found(h):
    report_id, _ = h.report()
    other_report, (foreign,) = h.report()
    before = h.rows(report_id)

    for proposal_id in (foreign, "doesnotexist"):
        resp = h.open_pr(report_id, {"fix_proposal_id": proposal_id})
        assert resp.status_code == 404
        assert resp.json()["code"] == "proposal_not_found"
    assert h.rows(report_id) == before
    assert h.github.calls == [] and h.git_calls == []


def test_branch_cannot_be_supplied(h):
    report_id, _ = h.report()
    resp = h.open_pr(report_id, {"branch": "main"})
    assert resp.status_code == 422
    assert h.rows(report_id)["report"]["status"] == "fix_proposed"


# ---------------------------------------------------------------------------
# 3. Restart recovery
# ---------------------------------------------------------------------------

def test_crash_after_claim_adopts_and_cleans_leftovers(h):
    report_id, (proposal_id,) = h.report()
    # Leftovers of a dead call: a registered worktree, a stray dir, a base ref.
    wt = Path(settings.repos_dir).resolve() / ".worktrees" / h.project_id / report_id
    wt.parent.mkdir(parents=True)
    git("worktree", "add", "-q", "--detach", str(wt), "HEAD", cwd=h.clone)
    (wt / "junk.txt").write_text("left behind")
    git("update-ref", f"refs/bugalizer/base/{report_id}", "HEAD", cwd=h.clone)
    conn = db._get_conn()
    conn.execute("UPDATE bug_reports SET status = 'fix_approved', claim_token = 'dead' WHERE id = ?",
                 (report_id,))
    conn.commit()

    resp = h.open_pr(report_id)

    assert resp.status_code == 201, resp.text
    assert h.pushes == 1 and h.pr_posts == 1
    assert h.rows(report_id)["report"]["status"] == "fix_committed"


def test_crash_after_pr_post_recovers_from_github(h):
    report_id, (proposal_id,) = h.report()
    first = h.open_pr(report_id).json()
    h.crash_after_pr(report_id)

    resp = h.open_pr(report_id)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {**first, "created": False}
    assert h.pushes == 1 and h.pr_posts == 1
    assert h.rows(report_id)["report"]["status"] == "fix_committed"


def test_crash_after_pr_post_recovers_when_merged_and_base_moved(h):
    report_id, (proposal_id,) = h.report()
    first = h.open_pr(report_id).json()
    h.crash_after_pr(report_id)
    h.github.merge(h.github.prs[0])
    h.advance_main()
    applies_before = len(h.git_args("apply"))

    resp = h.open_pr(report_id)

    assert resp.status_code == 200, resp.text
    assert resp.json()["pr_number"] == first["pr_number"]
    assert len(h.git_args("apply")) == applies_before  # no apply attempted
    assert h.pushes == 1 and h.pr_posts == 1


def test_finalization_is_atomic(h, monkeypatch):
    report_id, (proposal_id,) = h.report()

    original = db._record_pr_report

    def boom(*args, **kwargs):
        raise RuntimeError("injected failure mid-transaction")

    monkeypatch.setattr(db, "_record_pr_report", boom)
    failed = h.open_pr(report_id)
    assert failed.status_code == 500
    assert failed.json()["code"] == "internal_error"
    rows = h.rows(report_id)
    assert rows[proposal_id]["pr_url"] is None and rows[proposal_id]["pr_number"] is None
    assert rows[proposal_id]["pr_opened_at"] is None and rows[proposal_id]["status"] == "proposed"
    assert rows["report"]["status"] == "fix_proposed" and rows["report"]["claim_token"] is None
    monkeypatch.setattr(db, "_record_pr_report", original)

    recovered = h.open_pr(report_id)
    assert recovered.status_code == 200, recovered.text
    assert h.pushes == 1 and h.pr_posts == 1
    calls_before = len(h.github.calls)
    again = h.open_pr(report_id)
    assert again.status_code == 200
    assert len(h.github.calls) == calls_before  # committed: answered from the DB


@pytest.mark.parametrize("merged", [False, True], ids=["open", "merged-base-moved"])
@pytest.mark.parametrize("intent_lost", [False, True], ids=["intent", "trailers"])
def test_owner_survives_crash_across_proposals(h, merged, intent_lost):
    report_id, (a, b) = h.report(make_diff(FIX_A), make_diff(FIX_B))
    assert h.open_pr(report_id, {"fix_proposal_id": a}).status_code == 201
    h.crash_after_pr(report_id)
    if intent_lost:
        conn = db._get_conn()
        conn.execute("UPDATE fix_proposals SET pushed_sha = NULL WHERE id = ?", (a,))
        conn.commit()
    if merged:
        h.github.merge(h.github.prs[0])
        h.advance_main()
    state = h.rows(report_id)

    for body, expected_status in ((None, 200), ({"fix_proposal_id": b}, 409)):
        h.restore(state)
        pr_mod._active_claims.clear()
        resp = h.open_pr(report_id, body)
        assert resp.status_code == expected_status, resp.text
        assert resp.json()["fix_proposal_id"] == a
        if expected_status == 409:
            assert resp.json()["code"] == "pr_exists"
        rows = h.rows(report_id)
        assert rows[a]["pr_url"] and rows[a]["pr_number"] == 1 and rows[a]["pr_opened_at"]
        assert rows[b] == state[b]
        assert rows["report"]["status"] == "fix_committed"
        assert h.pushes == 1 and h.pr_posts == 1


def test_unattributed_pr_needs_an_explicit_proposal(h):
    report_id, (a, b) = h.report(make_diff(FIX_A), make_diff(FIX_B))
    h.push_foreign_branch(report_id)
    hand_pr = h.github.add_pr(f"fix/bugalizer-{report_id}")
    before = h.rows(report_id)

    resp = h.open_pr(report_id)

    assert resp.status_code == 409
    assert resp.json()["code"] == "pr_unattributed"
    assert resp.json()["pr_url"] == hand_pr["html_url"]
    assert resp.json()["pr_number"] == hand_pr["number"]
    after = h.rows(report_id)
    assert {k: v for k, v in after.items() if k != "report"} == \
        {k: v for k, v in before.items() if k != "report"}
    assert after["report"]["status"] == "fix_proposed" and after["report"]["claim_token"] is None

    named = h.open_pr(report_id, {"fix_proposal_id": b})
    assert named.status_code == 200, named.text
    assert named.json()["fix_proposal_id"] == b
    assert h.rows(report_id)[b]["pr_number"] == hand_pr["number"]
    assert h.rows(report_id)[a]["pr_url"] is None
    assert h.pushes == 0 and h.pr_posts == 0


def test_active_claim_is_not_adopted(h):
    report_id, _ = h.report()
    conn = db._get_conn()
    conn.execute("UPDATE bug_reports SET status = 'fix_approved', claim_token = 'live' WHERE id = ?",
                 (report_id,))
    conn.commit()
    pr_mod._active_claims[report_id] = "live"

    resp = h.open_pr(report_id)

    assert resp.status_code == 409 and resp.json()["code"] == "in_progress"
    assert h.rows(report_id)["report"]["claim_token"] == "live"
    assert h.git_calls == [] and h.github.calls == []


# ---------------------------------------------------------------------------
# 4. Failure, then retry on the same clone
# ---------------------------------------------------------------------------

def test_pr_post_failure_then_retry_resumes_without_push(h):
    report_id, (proposal_id,) = h.report()
    h.github.fail_post = (500, {"message": f"server error {TOKEN}"})

    failed = h.open_pr(report_id)
    assert failed.status_code == 502 and failed.json()["code"] == "github_error"
    rows = h.rows(report_id)
    assert rows["report"]["status"] == "fix_proposed" and rows["report"]["claim_token"] is None
    assert rows[proposal_id]["pushed_sha"] == h.bare_ref(f"refs/heads/fix/bugalizer-{report_id}")
    assert h.pushes == 1

    h.github.fail_post = None
    retried = h.open_pr(report_id)
    assert retried.status_code == 201, retried.text
    assert h.pushes == 1 and len(h.github.posted) == 1


def test_push_rejected_then_retry(h):
    report_id, _ = h.report()
    h.server.reject_push = True

    failed = h.open_pr(report_id)
    assert failed.status_code == 502 and failed.json()["code"] == "github_error"
    assert h.rows(report_id)["report"]["status"] == "fix_proposed"
    assert h.bare_ref(f"refs/heads/fix/bugalizer-{report_id}") is None

    h.server.reject_push = False
    assert h.open_pr(report_id).status_code == 201


def test_pr_already_exists_race_records_existing_pr(h):
    report_id, (proposal_id,) = h.report()
    assert h.open_pr(report_id).status_code == 201
    h.crash_after_pr(report_id)
    # The lookup misses the PR once (a race); the POST then reports it exists.
    h.github.hide_lookup_once = True

    resp = h.open_pr(report_id)

    assert resp.status_code == 200, resp.text
    assert resp.json()["fix_proposal_id"] == proposal_id
    assert h.pushes == 1
    assert h.rows(report_id)["report"]["status"] == "fix_committed"


# ---------------------------------------------------------------------------
# 5. Stale diff
# ---------------------------------------------------------------------------

def test_stale_diff_does_not_apply(h):
    report_id, _ = h.report()
    h.advance_main()
    refs_before = h.bare_refs()

    resp = h.open_pr(report_id)

    assert resp.status_code == 409 and resp.json()["code"] == "diff_does_not_apply"
    assert h.bare_refs() == refs_before
    assert h.github.calls == [("GET", f"/repos/{OWNER}/{REPO}/pulls")]
    assert h.pushes == 0
    assert h.rows(report_id)["report"]["status"] == "fix_proposed"


# ---------------------------------------------------------------------------
# 6. Write policy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ref", [
    "refs/heads/main", "main", "HEAD", "refs/heads/feature", "refs/heads/fix/other",
    "+refs/heads/fix/bugalizer-x", "refs/tags/fix/bugalizer-x",
])
def test_push_guard_refuses_forged_refs(ref):
    with pytest.raises(PermissionError):
        assert_push_allowed(ref, "main")


def test_push_guard_refuses_default_branch_even_with_prefix():
    with pytest.raises(PermissionError):
        assert_push_allowed("refs/heads/fix/bugalizer-x", "fix/bugalizer-x")
    assert_push_allowed("refs/heads/fix/bugalizer-abc", "main")


def test_foreign_branch_is_left_untouched(h):
    report_id, _ = h.report()
    h.push_foreign_branch(report_id)
    refs_before = h.bare_refs()

    resp = h.open_pr(report_id)

    assert resp.status_code == 409 and resp.json()["code"] == "branch_exists"
    assert h.bare_refs() == refs_before
    assert h.pushes == 0 and h.pr_posts == 0
    assert h.rows(report_id)["report"]["status"] == "fix_proposed"


def test_pushed_branch_for_other_proposal_is_branch_exists(h):
    report_id, (a, b) = h.report(make_diff(FIX_A), make_diff(FIX_B))
    h.github.fail_post = (500, {"message": "down"})
    assert h.open_pr(report_id, {"fix_proposal_id": a}).status_code == 502
    h.github.fail_post = None

    resp = h.open_pr(report_id, {"fix_proposal_id": b})

    assert resp.status_code == 409 and resp.json()["code"] == "branch_exists"
    assert resp.json()["fix_proposal_id"] == a
    assert h.pushes == 1 and len(h.github.posted) == 0


# ---------------------------------------------------------------------------
# 7. Destination and credentials
# ---------------------------------------------------------------------------

def test_remote_calls_use_canonical_url_and_env_credential(h):
    report_id, _ = h.report()
    config_before = (h.clone / ".git" / "config").read_bytes()

    assert h.open_pr(report_id).status_code == 201

    remote_calls = [c for c in h.git_calls if c["args"][0] in ("fetch", "ls-remote", "push")]
    assert {c["args"][0] for c in remote_calls} == {"fetch", "ls-remote", "push"}
    assert (h.clone / ".git" / "config").read_bytes() == config_before
    # Every request the module made to the git server was authorized.
    assert h.server.requests and all(r["authorized"] for r in h.server.requests)


def test_git_server_really_enforces_auth(h):
    """Control: without the header the server answers 401 and git fails."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG_")}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": ""})
    result = subprocess.run(
        ["git", "ls-remote", f"{h.server.url}/{OWNER}/{REPO}.git"],
        cwd=h.clone, env=env, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert any(not r["authorized"] for r in h.server.requests)
    h.server.requests.clear()


# ---------------------------------------------------------------------------
# 8. Concurrency
# ---------------------------------------------------------------------------

async def test_concurrent_calls_open_one_pr(h):
    report_id, _ = h.report()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        results = await asyncio.gather(
            client.post(f"/api/v1/reports/{report_id}/open-pr"),
            client.post(f"/api/v1/reports/{report_id}/open-pr"),
        )
    h.responses.extend(r.text for r in results)
    statuses = sorted(r.status_code for r in results)
    assert statuses == [201, 409], [r.text for r in results]
    loser = next(r for r in results if r.status_code == 409)
    assert loser.json()["code"] in ("in_progress", "wrong_status")
    assert h.pushes == 1 and h.pr_posts == 1


async def test_live_claim_cannot_be_patched_deleted_or_taken(h):
    """While a request is mid-flight, PATCH /status, DELETE and a second
    open-pr are all refused, and the first request still finalizes."""
    report_id, _ = h.report()
    entered, release = threading.Event(), threading.Event()
    h.block = ("fetch", entered, release)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        url = f"/api/v1/reports/{report_id}"
        first = asyncio.create_task(client.post(f"{url}/open-pr"))
        assert await asyncio.to_thread(entered.wait, 30)
        owner = pr_mod._active_claims[report_id]
        patched = await client.patch(f"{url}/status", json={"status": "fix_proposed"})
        second = await client.post(f"{url}/open-pr")
        deleted = await client.delete(url)
        mid_flight = h.rows(report_id)["report"]
        release.set()
        resp = await first
    h.responses.extend(r.text for r in (patched, second, deleted, resp))
    assert patched.status_code == 409
    assert second.status_code == 409 and second.json()["code"] == "in_progress"
    assert deleted.status_code == 409
    assert mid_flight["status"] == "fix_approved" and mid_flight["claim_token"] == owner
    assert resp.status_code == 201, resp.text
    assert h.rows(report_id)["report"]["status"] == "fix_committed"
    assert h.pushes == 1 and h.pr_posts == 1


@pytest.mark.parametrize("route", ["patch", "delete"])
async def test_public_mutation_started_before_the_claim_cannot_overwrite_it(h, monkeypatch, route):
    """PATCH / DELETE validate first, then pause right before their DB write;
    open-pr claims the row in that window. The stale public write is refused
    (409) and the open-pr owner still finalizes with one push and one PR."""
    from bugalizer.api import reports as reports_api

    report_id, _ = h.report()
    public_entered, public_release = threading.Event(), threading.Event()
    name = "report_update_status" if route == "patch" else "report_delete"
    real = getattr(reports_api, name)

    def held(*args, **kwargs):
        public_entered.set()
        assert public_release.wait(30)
        return real(*args, **kwargs)

    monkeypatch.setattr(reports_api, name, held)
    build_entered, build_release = threading.Event(), threading.Event()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        url = f"/api/v1/reports/{report_id}"
        if route == "patch":
            public = asyncio.create_task(client.patch(f"{url}/status", json={"status": "triaged"}))
        else:
            public = asyncio.create_task(client.delete(url))
        assert await asyncio.to_thread(public_entered.wait, 30)

        h.block = ("fetch", build_entered, build_release)
        owner = asyncio.create_task(client.post(f"{url}/open-pr"))
        assert await asyncio.to_thread(build_entered.wait, 30)
        token = pr_mod._active_claims[report_id]

        public_release.set()
        public_resp = await public
        after_public = h.rows(report_id)["report"]
        build_release.set()
        owner_resp = await owner
    h.responses.extend(r.text for r in (public_resp, owner_resp))

    assert public_resp.status_code == 409, public_resp.text
    assert after_public["status"] == "fix_approved" and after_public["claim_token"] == token
    assert after_public["resolution_reason"] is None
    assert owner_resp.status_code == 201, owner_resp.text
    assert h.rows(report_id)["report"]["status"] == "fix_committed"
    assert h.pushes == 1 and h.pr_posts == 1


@pytest.mark.parametrize("command", ["apply", "push"])
async def test_cancellation_waits_for_the_git_worker(h, command):
    """Cancelling a call while a git worker runs keeps the claim and the lock
    until the worker exits; only then does cleanup run and the claim go."""
    report_id, _ = h.report()
    entered, release = threading.Event(), threading.Event()
    h.block = (command, entered, release)
    task = asyncio.create_task(pr_mod.open_pull_request(report_id, None))
    assert await asyncio.to_thread(entered.wait, 30)
    started = len(h.git_calls)

    task.cancel()
    for _ in range(20):
        await asyncio.sleep(0.01)
    assert not task.done()
    assert len(h.git_calls) == started  # no cleanup while the worker is live
    assert h.rows(report_id)["report"]["status"] == "fix_approved"
    assert pr_mod.open_pr_running(report_id)
    with pytest.raises(pr_mod.OpenPrError) as retry:
        await pr_mod.open_pull_request(report_id, None)
    assert retry.value.code == "in_progress"

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    blocked_end = max(i for i, (kind, args) in enumerate(h.events)
                      if kind == "end" and args[0] == command)
    cleanup_starts = [i for i, (kind, args) in enumerate(h.events)
                      if kind == "start" and args[:2] == ("worktree", "remove")]
    assert cleanup_starts and min(i for i in cleanup_starts if i > started) > blocked_end
    assert all(i < started or i > blocked_end for i in cleanup_starts)
    assert h.rows(report_id)["report"]["status"] == "fix_proposed"
    assert not pr_mod.open_pr_running(report_id)
    assert h.pushes == (1 if command == "push" else 0)

    status, body = await pr_mod.open_pull_request(report_id, None)
    h.responses.append(json.dumps(body))
    assert status == 201
    assert h.pushes == 1 and h.pr_posts == 1  # a push-boundary retry resumes


def test_claim_rollback_is_internal_only():
    from bugalizer.models import BugStatus, validate_transition
    assert not validate_transition(BugStatus.FIX_APPROVED, BugStatus.FIX_PROPOSED)
    assert validate_transition(
        BugStatus.FIX_APPROVED, BugStatus.FIX_PROPOSED, enforce_phase_gating=False
    )


# ---------------------------------------------------------------------------
# 9. Secrecy (also checked in every test's teardown)
# ---------------------------------------------------------------------------

def test_github_422_is_redacted_502(h):
    report_id, _ = h.report()
    h.github.fail_post = (422, {"message": f"Validation Failed for token {TOKEN}"})
    resp = h.open_pr(report_id)
    assert resp.status_code == 502 and resp.json()["code"] == "github_error"
    assert "***" in h.caplog.text


def test_redact_strips_token_and_auth_header():
    text = f"x {TOKEN} y\nAUTHORIZATION: basic {TOKEN_B64}\nz {TOKEN_B64}"
    out = redact(text, TOKEN)
    assert TOKEN not in out and TOKEN_B64 not in out
    assert out.count("***") == 3


# ---------------------------------------------------------------------------
# 10. Isolation on the fetch-failure path
# ---------------------------------------------------------------------------

def test_fetch_failure_leaves_clone_untouched(h):
    report_id, _ = h.report()
    db.project_update(h.project_id, default_branch="does-not-exist")

    resp = h.open_pr(report_id)

    assert resp.status_code == 502 and resp.json()["code"] == "github_error"
    assert h.rows(report_id)["report"]["status"] == "fix_proposed"
    assert h.pushes == 0


# ---------------------------------------------------------------------------
# 11-12. Status gating, response shapes, configuration errors
# ---------------------------------------------------------------------------

def test_patch_status_cannot_enter_pr_states(h):
    report_id, _ = h.report()
    for target in ("fix_approved", "fix_committed"):
        resp = h.client.patch(f"/api/v1/reports/{report_id}/status", json={"status": target})
        assert resp.status_code == 409
    assert h.rows(report_id)["report"]["status"] == "fix_proposed"


def test_claim_token_is_never_exposed(h):
    report_id, _ = h.report()
    conn = db._get_conn()
    conn.execute("UPDATE bug_reports SET claim_token = 'secret-claim' WHERE id = ?", (report_id,))
    conn.commit()
    texts = [
        h.client.get(f"/api/v1/reports/{report_id}").text,
        h.client.get("/api/v1/reports").text,
        h.client.get(f"/api/v1/reports/{report_id}/fix_proposals").text,
    ]
    for text in texts:
        assert "claim_token" not in text and "secret-claim" not in text
    proposal = h.client.get(f"/api/v1/reports/{report_id}/fix_proposals").json()["fix_proposals"][0]
    assert {"pr_url", "pr_number", "pushed_sha", "pr_opened_at"} <= set(proposal)


def test_wrong_status_and_no_proposal(h):
    report_id, _ = h.report(status="triaged")
    resp = h.open_pr(report_id)
    assert resp.status_code == 409 and resp.json()["code"] == "wrong_status"

    bare_report = db.report_create(h.project_id, "t", "d", "r")
    db.report_update_status(bare_report["id"], "fix_proposed")
    resp = h.open_pr(bare_report["id"])
    assert resp.status_code == 409 and resp.json()["code"] == "no_proposal"

    assert h.client.post("/api/v1/reports/nope/open-pr").status_code == 404


def test_no_token_is_503(h, monkeypatch):
    report_id, _ = h.report()
    monkeypatch.setattr(settings, "github_token", None)
    resp = h.open_pr(report_id)
    assert resp.status_code == 503 and resp.json()["code"] == "github_not_configured"
    assert h.rows(report_id)["report"]["status"] == "fix_proposed"
    assert h.client.get("/health").json()["github_configured"] is False
    monkeypatch.setattr(settings, "github_token", SecretStr(TOKEN))
    assert h.client.get("/health").json()["github_configured"] is True


@pytest.mark.parametrize("change", [
    {"repo_url": "https://gitlab.com/o/r"},
    {"repo_path": None},
])
def test_non_github_or_uncloned_project_is_400(h, change):
    report_id, _ = h.report()
    db.project_update(h.project_id, **change)
    resp = h.open_pr(report_id)
    assert resp.status_code == 400
    assert h.rows(report_id)["report"]["status"] == "fix_proposed"
    assert h.git_calls == []


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://github.com/spherop/sonicgrid", ("spherop", "sonicgrid")),
    ("https://github.com/spherop/sonicgrid.git", ("spherop", "sonicgrid")),
    ("https://github.com/o/r/", ("o", "r")),
    ("git@github.com:spherop/sonicgrid.git", ("spherop", "sonicgrid")),
    ("git@github.com:o/r", ("o", "r")),
    ("https://gitlab.com/o/r", None),
    ("https://github.com/o/r/tree/main", None),
    ("https://github.com/../r", None),
    ("https://github.com/o/r%20x", None),
    ("http://github.com/o/r", None),
])
def test_parse_github_slug(url, expected):
    assert parse_github_slug(url) == expected


def test_strip_level_from_headers():
    assert _strip_level(make_diff(FIX_A)) == 1
    assert _strip_level(make_diff(FIX_A, prefixes=False)) == 0
    assert _strip_level("--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+x\n") == 1
    assert _strip_level("--- /dev/null\n+++ new.py\n@@ -0,0 +1 @@\n+x\n") == 0


def test_parse_numstat_z_handles_renames():
    out = "1\t1\tapp.py\x002\t0\t\x00old.py\x00new.py\x00"
    assert _parse_numstat_z(out) == ["app.py", "old.py", "new.py"]


def test_migration_adds_phase8_columns():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE bug_reports (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE fix_proposals (id TEXT PRIMARY KEY)")
    db._migrate(conn)
    reports = {r[1] for r in conn.execute("PRAGMA table_info(bug_reports)")}
    proposals = {r[1] for r in conn.execute("PRAGMA table_info(fix_proposals)")}
    assert "claim_token" in reports
    assert {"pr_url", "pr_number", "pushed_sha", "pr_opened_at"} <= proposals
