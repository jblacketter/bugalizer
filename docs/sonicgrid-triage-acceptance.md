# Sonicgrid triage-sync acceptance (Phase 11 / B3)

Post-merge walk on BOWIE, by Jack with Dan (the direction record's exit
criterion). Record the result in `docs/roadmap.md` under Phase 11. Setup details
are in [`deploy-windows.md` §7d](deploy-windows.md#7d-results-and-actions-in-sonicgrid-phase-11).

Prerequisites:

- B3 deployed on BOWIE (`/health` `revision` is the merge commit).
- S2 live in sonicgrid production: migration applied, `BUGALIZER_TRIAGE_TOKEN`
  set in Vercel production (S2 hosted acceptance, 2026-09-30).
- B1 configured on the sonicgrid project (`configure-sonicgrid-ingest.ps1`
  prints `CONFIGURED`).
- sonicgrid's open-action list is empty, or holds only actions you mean to
  run: the first tick claims and runs them.
- Jack and Dan are both sonicgrid admins.

```powershell
$B = "https://bugalizer.lan/api/v1"
$h = @{ "X-API-Key" = "<key>" }
$P = "<sonicgrid project id>"
```

1. **Configure.** In BOWIE's `.env`: `BUGALIZER_TRIAGE_SYNC_ENABLED=true`,
   `SONICGRID_TRIAGE_TOKEN=<same value as Vercel's BUGALIZER_TRIAGE_TOKEN>`,
   `BUGALIZER_SONICGRID_CLOUD_USERS=<Jack's sonicgrid email>`. Restart the
   service (`lan-mgr-bugalizer`). Then:
   `powershell -ExecutionPolicy Bypass -File scripts\windows\configure-sonicgrid-triage.ps1`
   must print `CONFIGURED`.
2. **Results appear.** Within one tick, `/admin/bugs` "Bugalizer triage" shows
   the imported bugs with Bugalizer's status, summary and severity, and for a
   localized bug the candidate files. `Invoke-RestMethod "$B/projects/$P/triage-sync" -Headers $h`:
   `results_pending: 0`, `result_errors: []`.
3. **Reporter view.** As a reporter who is not an admin, `/bugs` shows status,
   summary, severity and PR link only: no root cause, files or diff.
4. **Dan, local.** Dan queues "Analyze (local)" on a triaged bug. It shows as
   running, then done; the localization reaches the admin view.
5. **Dan, cloud.** Dan queues "Analyze (cloud)". It is refused, and the message
   says cloud analysis is limited to allowlisted users. Nothing ran on BOWIE.
6. **Jack, cloud.** Jack queues "Analyze (cloud)" on a localized bug. Done within
   one tick plus the run time; the root cause and diff appear in the admin view.
   `Invoke-RestMethod "$B/usage" -Headers $h` shows the run under
   `key_source: env`, `key_ref: sonicgrid:<Jack's user id>`. BOWIE's Stage 4 is
   `ollama` today, so this run is local and free; record which provider ran.
7. **Fix and open PR.** Needs B2's GitHub token (Phase 8, still pending). Jack
   queues "Fix and open PR". Either a PR on `fix/bugalizer-<id>` that a human
   reviews and merges on GitHub (record the URL), or an honest refusal such as
   `diff_does_not_apply` against the hand-refreshed clone, with step `fix: done`
   and its proposal id shown. Without the token: refused with
   `github_not_configured` after `fix: done`.
8. **Restart survival.** Queue "Analyze (local)", restart the service while it
   runs. After the restart the action is finished (done, or re-run and done),
   never left claimed past the 45-minute bound.

Record each step's outcome (token redacted).

## Phase 12 (B4): lanes, PR fate, reopen, per-user keys

Prerequisites: B4 deployed on BOWIE; sonicgrid S3a and S4 (board) live; S3b
(AI settings + credential endpoint) live for steps 13-15; B2's GitHub token
set for steps 10-12. Setup and the activation sequence:
[`deploy-windows.md` §7e](deploy-windows.md#7e-board-lanes-pr-fate-and-per-user-keys-phase-12).

9. **Lanes.** Within one tick of the upgrade every bug sits in a board lane
   that matches its Bugalizer status (`/health` `triage_sync.failing: 0`).
10. **Merged PR completes.** Merge a Bugalizer fix PR on GitHub. Within 5
    minutes plus one tick the card moves to Completed (`merged`) and the
    sonicgrid bug is resolved, with nobody touching the board.
11. **Closed PR returns.** Close another fix PR without merging. The card
    returns to Triaged (`pr_closed`) and the bug is active.
12. **Reopen.** Reopen the merged bug from step 10. It returns to Triaged and
    active, and stays there on later ticks (not re-closed). Reopen is not
    offered on a rejected or duplicate bug.
13. **Activate per-user keys** (§7e sequence). Record the stopped-process
    check from step 2 of that sequence.
14. **Own key.** Jack saves his key in AI settings and queues Analyze (cloud).
    Done; `Invoke-RestMethod "$B/usage" -Headers $h` shows the run under
    `key_source: request`, `key_ref: sonicgrid:<Jack's user id>`, and the
    Anthropic console shows the call on Jack's key, none on BOWIE's.
15. **No key.** An admin without a saved key cannot queue a paid action
    (refused in sonicgrid: "Add your Claude API key in AI settings").
