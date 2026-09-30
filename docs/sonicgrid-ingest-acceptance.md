# Sonicgrid ingest acceptance (Phase 10 / B1)

Post-merge walk on BOWIE with the real token (arbiter decision D-C). Record the
result in `docs/roadmap.md` under Phase 10. Setup details are in
[`deploy-windows.md` §7c](deploy-windows.md#7c-pulling-sonicgrid-bug-reports-phase-10).

Prerequisites: B1 deployed on BOWIE (`/health` `revision` is the merge commit);
`BUGALIZER_POLL_TOKEN` set in sonicgrid's Vercel production env (done in S0);
you are on sonicgrid's reporter list.

```powershell
$B = "https://bugalizer.lan/api/v1"
$h = @{ "X-API-Key" = "<key>" }
$P = "<sonicgrid project id>"
```

1. **Configure.** In BOWIE's `.env`: `BUGALIZER_INGEST_ENABLED=true`,
   `SONICGRID_POLL_TOKEN=<same value as Vercel's BUGALIZER_POLL_TOKEN>`. Restart
   the service (LAN Service Manager, `lan-mgr-bugalizer`). Then set the
   project's ingest config and run a first poll:
   `powershell -ExecutionPolicy Bypass -File scripts\windows\configure-sonicgrid-ingest.ps1`
   (must print `CONFIGURED`; this also covers step 2).
2. **Credential and first poll.**
   `Invoke-RestMethod "$B/projects/$P/ingest" -Headers $h` shows
   `credential_present: true`. Within one interval (2 min), or after
   `Invoke-RestMethod -Method Post "$B/projects/$P/ingest/run" -Headers $h`:
   `last_ok_at` set, `last_error` null. Any reports already active in sonicgrid
   are imported now (`imported_total`).
3. **A new bug arrives.** File a bug through sonicgrid's Report Bug dialog. Within
   one interval it is on the dashboard with label `sonicgrid`; its
   `external_id` equals the sonicgrid report id (`/admin/bugs`), the reporter is
   the name only, and it moves from `submitted` through Stage 1 to triage.
4. **No duplicate.**
   `Invoke-RestMethod -Method Post "$B/projects/$P/ingest/run?full=true" -Headers $h`
   answers `imported: 0` (repeat until `rewalk_in_progress` is false on the
   status endpoint if there are many reports).
5. **Clean up.** Resolve the test bug in sonicgrid `/admin/bugs`, then close
   Bugalizer's copy by hand (sonicgrid resolution is not synced, by design).

Keep `BUGALIZER_AUTO_FIX_ENABLED=false` on BOWIE: imported reports are
`analysis_mode=auto` and would otherwise be eligible for paid Stage 4 runs.

## Result

_Not yet run._
