"""Open a pull request from a fix proposal (Phase 8 / B2, `docs/phases/open-pr.md`).

This is the only code in Bugalizer that writes to a remote. Write policy:

- The branch is always `fix/bugalizer-<report-id>`, computed here. A guard
  refuses any push to the default branch or to a ref without that prefix.
- Never force: no `--force`, `--force-with-lease` or `+refspec` on any fetch,
  push or ref update. (`git worktree remove --force` deletes a local scratch
  directory and is the one exception.)
- No local branch is ever created. The commit is built detached in a
  throwaway worktree and pushed as `HEAD:refs/heads/fix/bugalizer-<id>`.
- One branch and one PR per report, looked up (DB, then GitHub) before any
  apply, commit or push.
- Never merge. A human merges on GitHub.

Every remote git command names the canonical HTTPS URL
(`<github_web_base>/<owner>/<repo>.git`), never `origin`, and carries the token
only through the environment (`GIT_CONFIG_*` extraheader, empty
`credential.helper`). The token never reaches a URL, argv, git config, log
line, exception message or response.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

from bugalizer import db
from bugalizer.config import settings
from bugalizer.models import BugStatus, OpenPrResponse, validate_transition

logger = logging.getLogger(__name__)

BRANCH_PREFIX = "fix/bugalizer-"
BASE_REF_PREFIX = "refs/bugalizer/base/"
PR_BODY_FOOTER = "Opened by Bugalizer. A human reviews and merges; Bugalizer never merges."

_MAX_PR_BODY_CHARS = 60_000
_GIT_TIMEOUT_SECONDS = 300

# Requests currently holding an open-pr claim in this process:
# {report_id: claim_token}. Bugalizer runs as one process per database
# (decision log, 2026-09-17), so a `fix_approved` report whose token is not
# here has lost its owner and may be adopted.
_active_claims: dict[str, str] = {}

# Serializes git work per project (worktrees share the analysis clone).
_project_locks: dict[str, asyncio.Lock] = {}

# Test seam: an httpx transport for the GitHub REST client. None = network.
_http_transport: Optional[httpx.AsyncBaseTransport] = None


def open_pr_running(report_id: str) -> bool:
    """True while a request in this process holds the report's open-pr claim."""
    return report_id in _active_claims


class OpenPrError(Exception):
    """A handled open-pr outcome with an HTTP status and a stable code."""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.message, **self.extra}


class _GitHubError(Exception):
    """A GitHub REST failure. The message is fixed text plus the status."""


class _PrAlreadyExists(Exception):
    """POST /pulls answered 422 "a pull request already exists"."""


# ---------------------------------------------------------------------------
# Destination
# ---------------------------------------------------------------------------

_SLUG_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
_HTTPS_GITHUB = re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$")
_SSH_GITHUB = re.compile(r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_SAFE_BRANCH = re.compile(r"^[A-Za-z0-9._/-]+$")


def parse_github_slug(repo_url: str) -> Optional[tuple[str, str]]:
    """`(owner, repo)` for a github.com HTTPS or SSH URL, else None."""
    url = (repo_url or "").strip()
    match = _HTTPS_GITHUB.match(url) or _SSH_GITHUB.match(url)
    if not match:
        return None
    owner, repo = match.group(1), match.group(2)
    for part in (owner, repo):
        if not _SLUG_PART.match(part) or part in (".", ".."):
            return None
    return owner, repo


def canonical_url(owner: str, repo: str) -> str:
    """The only remote this module talks to (never `origin`)."""
    return f"{settings.github_web_base.rstrip('/')}/{owner}/{repo}.git"


def branch_for(report_id: str) -> str:
    return f"{BRANCH_PREFIX}{report_id}"


def assert_push_allowed(target_ref: str, default_branch: str) -> None:
    """Write-policy guard: refuse the default branch and unprefixed refs."""
    if target_ref == f"refs/heads/{default_branch}" or target_ref in (default_branch, "HEAD"):
        raise PermissionError("push to the default branch is refused")
    if not target_ref.startswith(f"refs/heads/{BRANCH_PREFIX}"):
        raise PermissionError("push outside refs/heads/fix/bugalizer-* is refused")
    if target_ref.startswith("+") or ":" in target_ref:
        raise PermissionError("forced or compound push refspecs are refused")


def _worktree_root() -> Path:
    return (Path(settings.repos_dir).resolve() / ".worktrees")


def _worktree_path(project_id: str, report_id: str) -> Path:
    path = (_worktree_root() / project_id / report_id).resolve()
    if _worktree_root() not in path.parents:
        raise RuntimeError("worktree path escapes the worktree root")
    return path


# ---------------------------------------------------------------------------
# Git plumbing
# ---------------------------------------------------------------------------

def _run_git(
    args: list[str],
    cwd: str,
    *,
    env: dict[str, str],
    input: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Run one git command. Every git call of this module goes through here."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        input=input,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )


def _local_env() -> dict[str, str]:
    """Process env minus anything that could redirect git or prompt for input."""
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("GIT_CONFIG_")
        and k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                      "GIT_ASKPASS", "SSH_ASKPASS", "GIT_CONFIG_PARAMETERS")
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _auth_header(token: str) -> str:
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return f"AUTHORIZATION: basic {encoded}"


def _remote_env(token: str) -> dict[str, str]:
    """Env-only credential for the canonical remote (never argv or config)."""
    env = _local_env()
    env.update({
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": f"http.{settings.github_web_base.rstrip('/')}/.extraheader",
        "GIT_CONFIG_VALUE_0": _auth_header(token),
        "GIT_CONFIG_KEY_1": "credential.helper",
        "GIT_CONFIG_VALUE_1": "",
    })
    return env


def _commit_env() -> dict[str, str]:
    env = _local_env()
    env.update({
        "GIT_AUTHOR_NAME": settings.commit_author_name,
        "GIT_AUTHOR_EMAIL": settings.commit_author_email,
        "GIT_COMMITTER_NAME": settings.commit_author_name,
        "GIT_COMMITTER_EMAIL": settings.commit_author_email,
    })
    return env


_AUTH_LINE = re.compile(r"(?i)(authorization:\s*)[^\r\n]*")


def redact(text: Optional[str], token: Optional[str]) -> str:
    """Strip the token, its basic-auth encoding and any auth header value."""
    if not text:
        return ""
    out = text
    if token:
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        for secret in (encoded, token):
            out = out.replace(secret, "***")
    return _AUTH_LINE.sub(r"\1***", out)


def _strip_level(diff: str) -> int:
    """`1` when the diff's file headers carry a/ b/ prefixes, else `0`."""
    minus: Optional[str] = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            return 1
        if line.startswith("--- ") and minus is None:
            minus = line[4:].split("\t")[0].strip().strip('"')
            continue
        if line.startswith("+++ ") and minus is not None:
            plus = line[4:].split("\t")[0].strip().strip('"')
            path = plus if minus == "/dev/null" else minus
            return 1 if path.startswith(("a/", "b/")) else 0
    return 1


def _parse_numstat_z(out: str) -> list[str]:
    """Paths from `git apply --numstat -z` (renames yield both paths)."""
    parts = out.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(parts):
        fields = parts[i].split("\t")
        if len(fields) == 3 and fields[2]:
            paths.append(fields[2])
            i += 1
        elif len(fields) == 3:
            paths.extend(p for p in parts[i + 1:i + 3] if p)
            i += 3
        else:
            i += 1
    return paths


def _trailer(message: str, key: str) -> Optional[str]:
    match = re.search(rf"^{re.escape(key)}:\s*(\S+)\s*$", message or "", re.MULTILINE)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# GitHub REST client
# ---------------------------------------------------------------------------

class _GitHub:
    def __init__(self, token: str, owner: str, repo: str) -> None:
        self._token = token
        self.owner = owner
        self.repo = repo

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=settings.github_api_base.rstrip("/"),
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "bugalizer",
            },
            timeout=30.0,
            transport=_http_transport,
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            async with self._client() as client:
                return await client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            logger.warning("GitHub %s %s failed: %s", method, path, type(exc).__name__)
            raise _GitHubError(f"GitHub API request failed ({type(exc).__name__})") from None

    def _fail(self, method: str, path: str, resp: httpx.Response) -> _GitHubError:
        message = ""
        try:
            message = str(resp.json().get("message", ""))
        except Exception:
            pass
        logger.warning(
            "GitHub %s %s answered %d: %s",
            method, path, resp.status_code, redact(message, self._token)[:300],
        )
        return _GitHubError(f"GitHub API answered {resp.status_code}")

    async def find_pr(self, branch: str) -> Optional[dict[str, Any]]:
        """The PR (any state) whose head is `owner:branch`, or None."""
        path = f"/repos/{self.owner}/{self.repo}/pulls"
        resp = await self._request(
            "GET", path,
            params={"head": f"{self.owner}:{branch}", "state": "all", "per_page": 100},
        )
        if resp.status_code != 200:
            raise self._fail("GET", path, resp)
        for pr in resp.json():
            if (pr.get("head") or {}).get("ref") == branch:
                return pr
        return None

    async def pr_commits(self, number: int) -> list[dict[str, Any]]:
        path = f"/repos/{self.owner}/{self.repo}/pulls/{number}/commits"
        resp = await self._request("GET", path, params={"per_page": 100})
        if resp.status_code != 200:
            raise self._fail("GET", path, resp)
        return list(resp.json())

    async def create_pr(self, *, title: str, body: str, head: str, base: str) -> dict[str, Any]:
        path = f"/repos/{self.owner}/{self.repo}/pulls"
        resp = await self._request(
            "POST", path,
            json={"title": title, "body": body, "head": head, "base": base, "draft": False},
        )
        if resp.status_code == 422 and "already exists" in resp.text.lower():
            raise _PrAlreadyExists()
        if resp.status_code != 201:
            raise self._fail("POST", path, resp)
        return resp.json()


# ---------------------------------------------------------------------------
# One open-pr call
# ---------------------------------------------------------------------------

async def _in_thread(fn: Any, *args: Any) -> Any:
    """Run blocking git work in a worker thread that cancellation cannot orphan.

    Cancelling an await of `asyncio.to_thread` does not stop the thread or
    its git subprocess. So on cancellation this waits (absorbing repeated
    cancellations) until the worker has finished, and only then re-raises.
    The caller keeps the project lock and the claim until that point, so no
    cleanup or retry can overlap a live worker.
    """
    worker = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    cancelled = False
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled = True  # only we can be cancelled; the shielded worker runs on
        except Exception:
            pass  # the worker's own failure, raised by result() below
    if cancelled:
        raise asyncio.CancelledError()
    return worker.result()

@dataclass
class _Call:
    report: dict[str, Any]
    project: dict[str, Any]
    owner: str
    repo: str
    token: str
    clone: str
    default_branch: str
    claim_token: str
    requested_id: Optional[str]
    proposal: dict[str, Any]
    worktree: Path
    finalized: bool = False
    proposals: list[dict[str, Any]] = field(default_factory=list)

    @property
    def report_id(self) -> str:
        return self.report["id"]

    @property
    def branch(self) -> str:
        return branch_for(self.report_id)

    @property
    def base_ref(self) -> str:
        return f"{BASE_REF_PREFIX}{self.report_id}"

    @property
    def canonical(self) -> str:
        return canonical_url(self.owner, self.repo)


def _answer(proposal: dict[str, Any], *, created: bool) -> dict[str, Any]:
    return OpenPrResponse(
        pr_url=proposal["pr_url"],
        pr_number=int(proposal["pr_number"]),
        branch=proposal.get("branch_name") or branch_for(proposal["bug_report_id"]),
        fix_proposal_id=proposal["id"],
        created=created,
    ).model_dump()


def _recorded_answer(
    recorded: dict[str, Any], requested_id: Optional[str]
) -> tuple[int, dict[str, Any]]:
    """Scope 1: a recorded PR answers 200 for its owner, else 409 pr_exists."""
    if requested_id is not None and requested_id != recorded["id"]:
        raise OpenPrError(
            409, "pr_exists",
            "This report already has a pull request, opened from another proposal",
            pr_url=recorded["pr_url"], pr_number=int(recorded["pr_number"]),
            fix_proposal_id=recorded["id"],
        )
    return 200, _answer(recorded, created=False)


async def open_pull_request(
    report_id: str, requested_proposal_id: Optional[str]
) -> tuple[int, dict[str, Any]]:
    """Run one open-pr call for an existing report. Returns (status, body) or
    raises OpenPrError. The checks before the claim never mutate anything."""
    report = db.report_get(report_id)
    if report is None:
        raise OpenPrError(404, "report_not_found", "Bug report not found")

    requested: Optional[dict[str, Any]] = None
    if requested_proposal_id is not None:
        requested = db.fix_proposal_of_report(report_id, requested_proposal_id)
        if requested is None:
            raise OpenPrError(404, "proposal_not_found", "Fix proposal not found for this report")

    recorded = db.recorded_pull_request(report_id)
    if recorded is not None:
        return _recorded_answer(recorded, requested_proposal_id)

    proposals = db.fix_proposals_for_report(report_id)
    if not proposals:
        raise OpenPrError(409, "no_proposal", "The report has no fix proposal")
    proposal = requested or proposals[0]

    # A running request owns the report whatever its row says.
    if open_pr_running(report_id):
        raise OpenPrError(409, "in_progress", "An open-pr request for this report is running")
    status = report["status"]
    if status not in (BugStatus.FIX_PROPOSED.value, BugStatus.FIX_APPROVED.value):
        raise OpenPrError(
            409, "wrong_status",
            f"Report is in status '{status}'; open-pr requires 'fix_proposed'",
        )

    token = settings.github_token_value()
    if token is None:
        raise OpenPrError(503, "github_not_configured", "BUGALIZER_GITHUB_TOKEN is not set")

    project = db.project_get(report["project_id"]) or {}
    slug = parse_github_slug(project.get("repo_url", ""))
    if slug is None:
        raise OpenPrError(400, "not_github", "The project repo is not a github.com repository")
    clone = project.get("repo_path")
    if not clone or not (Path(clone) / ".git").exists():
        raise OpenPrError(400, "not_cloned", "Project repo not cloned. Run POST /clone first.")
    default_branch = project.get("default_branch") or "main"
    if not _SAFE_BRANCH.match(default_branch) or ".." in default_branch:
        raise OpenPrError(400, "bad_default_branch", "The project's default branch name is invalid")
    if not _SAFE_ID.match(report_id) or not _SAFE_ID.match(project["id"]):
        raise OpenPrError(400, "bad_id", "Report or project id is not path-safe")

    # Claim: fix_proposed -> fix_approved with a fresh token, or adopt a claim
    # whose owner is gone. No await between the checks above and this CAS.
    assert validate_transition(
        BugStatus.FIX_PROPOSED, BugStatus.FIX_APPROVED, enforce_phase_gating=False
    )
    claim_token = uuid.uuid4().hex
    if status == BugStatus.FIX_APPROVED.value:
        won = db.adopt_claim(report_id, report.get("claim_token"), claim_token)
    else:
        won = db.try_claim_report(
            report_id, BugStatus.FIX_PROPOSED.value, BugStatus.FIX_APPROVED.value,
            claim_token=claim_token,
        )
    if not won:
        current = db.report_get(report_id) or {}
        if current.get("status") == BugStatus.FIX_APPROVED.value:
            raise OpenPrError(409, "in_progress", "An open-pr request for this report is running")
        raise OpenPrError(409, "wrong_status", "The report changed status; retry")
    if status == BugStatus.FIX_APPROVED.value:
        logger.info("open-pr: adopted an abandoned claim on report %s", report_id)
    _active_claims[report_id] = claim_token

    call = _Call(
        report=report, project=project, owner=slug[0], repo=slug[1], token=token,
        clone=str(Path(clone).resolve()), default_branch=default_branch,
        claim_token=claim_token, requested_id=requested_proposal_id, proposal=proposal,
        worktree=_worktree_path(project["id"], report_id),
    )
    try:
        lock = _project_locks.setdefault(project["id"], asyncio.Lock())
        async with lock:
            try:
                await _in_thread(_cleanup, call)
                return await _run_claimed(call)
            finally:
                # Every git worker this call dispatched has finished here
                # (_in_thread drains them), so the cleanup cannot race one.
                try:
                    await _in_thread(_cleanup, call)
                except asyncio.CancelledError:
                    pass  # _in_thread drained the cleanup worker first
                except Exception as exc:
                    logger.warning(
                        "open-pr: cleanup for report %s failed: %s",
                        report_id, redact(str(exc), token),
                    )
    finally:
        if not call.finalized:
            try:
                db.release_claim(report_id, claim_token)
            except Exception as exc:  # the next call adopts the claim
                logger.error(
                    "open-pr: releasing the claim on report %s failed: %s",
                    report_id, type(exc).__name__,
                )
        if _active_claims.get(report_id) == claim_token:
            del _active_claims[report_id]


async def _run_claimed(call: _Call) -> tuple[int, dict[str, Any]]:
    call.proposals = db.fix_proposals_for_report(call.report_id)
    github = _GitHub(call.token, call.owner, call.repo)

    # Step 5: reconcile with GitHub before any apply, commit or push.
    try:
        pr = await github.find_pr(call.branch)
    except _GitHubError as exc:
        raise OpenPrError(502, "github_error", str(exc)) from None
    if pr is not None:
        return await _record_existing_pr(call, github, pr)

    tip = await _in_thread(_remote_branch_tip, call)
    if tip is not None:
        pusher = next((p for p in call.proposals if p.get("pushed_sha") == tip), None)
        if pusher is None:
            raise OpenPrError(
                409, "branch_exists",
                f"Branch {call.branch} exists on the remote and was not pushed by "
                "Bugalizer; it is left untouched (see deploy-windows.md)",
                branch=call.branch,
            )
        if call.requested_id is not None and call.requested_id != pusher["id"]:
            raise OpenPrError(
                409, "branch_exists",
                f"Branch {call.branch} was pushed for another proposal",
                branch=call.branch, fix_proposal_id=pusher["id"],
            )
        logger.info("open-pr: branch %s already pushed; resuming at the PR", call.branch)
        return await _create_pr(call, github, pusher, tip)

    # Steps 6-9: build the commit, record the intent, push, open the PR.
    sha = await _in_thread(_build_commit, call)
    db.fix_proposal_record_push_intent(call.proposal["id"], sha, call.branch)
    await _in_thread(_push, call)
    return await _create_pr(call, github, call.proposal, sha)


def _resolve_owner(
    call: _Call, pr: dict[str, Any], commits: list[dict[str, Any]]
) -> Optional[str]:
    """The proposal that opened `pr`, from durable data only."""
    shas = {c.get("sha") for c in commits} | {(pr.get("head") or {}).get("sha")}
    shas.discard(None)
    by_intent = [p for p in call.proposals if p.get("pushed_sha") and p["pushed_sha"] in shas]
    if len(by_intent) == 1:
        return by_intent[0]["id"]
    ids = {p["id"] for p in call.proposals}
    for commit in commits:
        message = (commit.get("commit") or {}).get("message", "")
        proposal_id = _trailer(message, "Bugalizer-Proposal")
        if _trailer(message, "Bugalizer-Report") == call.report_id and proposal_id in ids:
            return proposal_id
    return None


async def _record_existing_pr(
    call: _Call, github: _GitHub, pr: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    try:
        commits = await github.pr_commits(int(pr["number"]))
    except _GitHubError as exc:
        raise OpenPrError(502, "github_error", str(exc)) from None
    owner_id = _resolve_owner(call, pr, commits)
    if owner_id is None:
        if call.requested_id is None:
            raise OpenPrError(
                409, "pr_unattributed",
                "A pull request exists for this report's branch, but the proposal it "
                "implements cannot be established; call again naming fix_proposal_id",
                pr_url=pr.get("html_url"), pr_number=int(pr["number"]),
            )
        owner_id = call.requested_id
    recorded = _record(call, owner_id, pr, pushed_sha=None)
    if call.requested_id is not None and call.requested_id != owner_id:
        error = OpenPrError(
            409, "pr_exists",
            "This report already has a pull request, opened from another proposal",
            pr_url=recorded["pr_url"], pr_number=int(recorded["pr_number"]),
            fix_proposal_id=owner_id,
        )
        return 409, error.body()
    return 200, _answer(recorded, created=False)


async def _create_pr(
    call: _Call, github: _GitHub, proposal: dict[str, Any], sha: str
) -> tuple[int, dict[str, Any]]:
    title = " ".join(call.report["title"].split())[:200]
    try:
        pr = await github.create_pr(
            title=f"fix: {title} (bugalizer {call.report_id})",
            body=_pr_body(call.report, proposal),
            head=call.branch,
            base=call.default_branch,
        )
    except _PrAlreadyExists:
        # A race with our own earlier attempt: record what GitHub has.
        try:
            existing = await github.find_pr(call.branch)
        except _GitHubError as exc:
            raise OpenPrError(502, "github_error", str(exc)) from None
        if existing is None:
            raise OpenPrError(502, "github_error", "GitHub reported an existing PR it cannot find")
        return await _record_existing_pr(call, github, existing)
    except _GitHubError as exc:
        raise OpenPrError(502, "github_error", str(exc)) from None
    recorded = _record(call, proposal["id"], pr, pushed_sha=sha)
    logger.info("open-pr: opened %s for report %s", recorded["pr_url"], call.report_id)
    return 201, _answer(recorded, created=True)


def _record(
    call: _Call, proposal_id: str, pr: dict[str, Any], *, pushed_sha: Optional[str]
) -> dict[str, Any]:
    try:
        db.record_pull_request(
            call.report_id, proposal_id, call.claim_token,
            pr_url=pr["html_url"], pr_number=int(pr["number"]),
            pushed_sha=pushed_sha, branch_name=call.branch,
        )
    except db.ClaimLostError:
        raise OpenPrError(409, "in_progress", "The open-pr claim was lost; retry") from None
    call.finalized = True
    recorded = db.fix_proposal_of_report(call.report_id, proposal_id)
    assert recorded is not None
    return recorded


def _pr_body(report: dict[str, Any], proposal: dict[str, Any]) -> str:
    analysis = db.fix_analysis_for_proposal(proposal)
    files = proposal.get("files_changed") or []
    if not isinstance(files, list):
        files = [str(files)]
    confidence = proposal.get("confidence")
    if analysis and analysis.get("llm_provider"):
        tier = "local" if analysis["llm_provider"] == "ollama" else "cloud"
        produced_by = f"{tier} tier, `{analysis['llm_provider']}/{analysis.get('llm_model') or '?'}`"
    else:
        produced_by = "unknown"
    lines = [
        f"Bugalizer report `{report['id']}`: {report['title']}",
        "",
        "## Root cause",
        proposal.get("root_cause") or "(none recorded)",
        "",
        "## Explanation",
        proposal.get("explanation") or "(none recorded)",
        "",
        f"**Confidence:** {confidence:.2f}" if isinstance(confidence, (int, float)) else "**Confidence:** unknown",
        "",
        "## Files changed",
        *([f"- `{f}`" for f in files] or ["(none listed)"]),
        "",
        f"**Produced by:** {produced_by}",
        f"**Fix proposal:** `{proposal['id']}`",
        "",
        "---",
        PR_BODY_FOOTER,
    ]
    body = "\n".join(lines)
    if len(body) > _MAX_PR_BODY_CHARS:
        body = body[:_MAX_PR_BODY_CHARS - len(PR_BODY_FOOTER) - 20] + "\n\n(truncated)\n\n" + PR_BODY_FOOTER
    return body


# ---------------------------------------------------------------------------
# Git steps (run in a worker thread, under the project lock)
# ---------------------------------------------------------------------------

def _cleanup(call: _Call) -> None:
    """Remove this report's worktree and base ref, from this or a dead call.

    Both are keyed by report id and only the claim holder touches them.
    """
    env = _local_env()
    path = call.worktree
    root = _worktree_root()
    if root not in path.parents:
        raise RuntimeError("worktree path escapes the worktree root")
    listed = _run_git(["worktree", "list", "--porcelain"], call.clone, env=env)
    registered = any(
        line.startswith("worktree ") and Path(line[len("worktree "):]).resolve() == path
        for line in listed.stdout.splitlines()
    )
    if registered or path.exists():
        _run_git(["worktree", "remove", "--force", str(path)], call.clone, env=env)
        _run_git(["worktree", "prune"], call.clone, env=env)
        if path.exists():
            shutil.rmtree(path)
    if _run_git(["show-ref", "--verify", "--quiet", call.base_ref], call.clone, env=env).returncode == 0:
        _run_git(["update-ref", "-d", call.base_ref], call.clone, env=env)


def _remote_branch_tip(call: _Call) -> Optional[str]:
    result = _run_git(
        ["ls-remote", call.canonical, f"refs/heads/{call.branch}"],
        call.clone, env=_remote_env(call.token),
    )
    if result.returncode != 0:
        logger.warning("open-pr: ls-remote failed: %s", redact(result.stderr, call.token)[:500])
        raise OpenPrError(502, "github_error", "Listing the remote branch failed")
    for line in result.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == f"refs/heads/{call.branch}":
            return sha.strip()
    return None


def _git_or_500(result: subprocess.CompletedProcess, what: str, token: str) -> None:
    if result.returncode != 0:
        logger.error("open-pr: git %s failed: %s", what, redact(result.stderr, token)[:500])
        raise OpenPrError(500, "git_error", f"Local git {what} failed")


def _build_commit(call: _Call) -> str:
    """Fetch the base, apply the diff in a detached worktree, commit. Returns the SHA."""
    env = _local_env()
    fetch = _run_git(
        ["fetch", "--no-tags", "--no-write-fetch-head", call.canonical,
         f"refs/heads/{call.default_branch}:{call.base_ref}"],
        call.clone, env=_remote_env(call.token),
    )
    if fetch.returncode != 0:
        stderr = redact(fetch.stderr, call.token)
        logger.warning("open-pr: base fetch failed: %s", stderr[:500])
        if "no-write-fetch-head" in stderr:
            raise OpenPrError(502, "github_error", "git 2.29 or later is required (--no-write-fetch-head)")
        raise OpenPrError(502, "github_error", "Fetching the default branch failed")

    base = _run_git(["rev-parse", "--verify", f"{call.base_ref}^{{commit}}"], call.clone, env=env)
    _git_or_500(base, "rev-parse", call.token)
    call.worktree.parent.mkdir(parents=True, exist_ok=True)
    added = _run_git(
        ["worktree", "add", "--detach", str(call.worktree), base.stdout.strip()],
        call.clone, env=env,
    )
    _git_or_500(added, "worktree add", call.token)

    wt = str(call.worktree)
    diff = call.proposal.get("diff") or ""
    if not diff.endswith("\n"):
        diff += "\n"
    strip = f"-p{_strip_level(diff)}"
    check = _run_git(["apply", "--check", strip, "-"], wt, env=env, input=diff)
    if not diff.strip() or check.returncode != 0:
        logger.info("open-pr: diff does not apply: %s", redact(check.stderr, call.token)[:500])
        raise OpenPrError(
            409, "diff_does_not_apply",
            "The proposed diff does not apply to the current default branch",
        )
    numstat = _run_git(["apply", "--numstat", "-z", strip, "-"], wt, env=env, input=diff)
    _git_or_500(numstat, "apply --numstat", call.token)
    paths = _parse_numstat_z(numstat.stdout)
    _git_or_500(_run_git(["apply", strip, "-"], wt, env=env, input=diff), "apply", call.token)
    _git_or_500(
        _run_git(["--literal-pathspecs", "add", "-A", "--", *paths], wt, env=env),
        "add", call.token,
    )
    if _run_git(["diff", "--cached", "--quiet"], wt, env=env).returncode == 0:
        raise OpenPrError(409, "diff_does_not_apply", "The proposed diff makes no change")

    title = " ".join(call.report["title"].split())[:200]
    message = (
        f"fix: {title} (bugalizer {call.report_id})\n\n"
        f"{(call.proposal.get('root_cause') or '').strip()[:2000]}\n\n"
        f"Bugalizer-Report: {call.report_id}\n"
        f"Bugalizer-Proposal: {call.proposal['id']}\n"
    )
    commit = _run_git(
        ["-c", "commit.gpgsign=false", "commit", "--no-verify", "-q", "-F", "-"],
        wt, env=_commit_env(), input=message,
    )
    _git_or_500(commit, "commit", call.token)
    head = _run_git(["rev-parse", "HEAD"], wt, env=env)
    _git_or_500(head, "rev-parse", call.token)
    return head.stdout.strip()


def _push(call: _Call) -> None:
    target = f"refs/heads/{call.branch}"
    assert_push_allowed(target, call.default_branch)
    result = _run_git(
        ["push", call.canonical, f"HEAD:{target}"],
        str(call.worktree), env=_remote_env(call.token),
    )
    if result.returncode == 0:
        return
    stderr = redact(result.stderr, call.token)
    logger.warning("open-pr: push failed: %s", stderr[:500])
    if any(s in stderr for s in ("non-fast-forward", "(fetch first)", "(already exists)")):
        raise OpenPrError(
            409, "branch_exists",
            f"Branch {call.branch} appeared on the remote; it is left untouched",
            branch=call.branch,
        )
    raise OpenPrError(502, "github_error", "Pushing the branch failed")
