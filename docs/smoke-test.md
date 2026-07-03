# LAN smoke test (Phase 5 §5.5 acceptance)

End-to-end check that the deployed service works from **another machine on
the LAN** against **real Ollama**, including one paid cloud escalation.
Record the result (date, machine, pass/fail per step) in
`docs/decision_log.md` — that closes Phase 5's definition of done.

Prerequisites: service deployed per [`deploy-windows.md`](deploy-windows.md),
`BUGALIZER_API_KEYS` set, `qwen2.5-coder:7b` pulled, Anthropic key set.

Run every step **from a different machine** than the host (e.g. the Mac):

```bash
HOST=http://192.168.68.xx:8090     # the Windows box
KEY=<api key>
```

## 1. Reachability

```bash
curl -s $HOST/health/live     # -> {"status": "ok", ...}
curl -s $HOST/health          # -> checks.database=true, checks.ollama=true, checks.worker=true
```

Also confirm a request **without** `X-API-Key` gets `401` on
`$HOST/api/v1/reports` (auth is actually on).

## 2. Project setup

```bash
curl -s -X POST $HOST/api/v1/projects -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name": "smoke", "repo_url": "<a real small repo>"}'
# note the "id" -> PROJECT
curl -s -X POST $HOST/api/v1/projects/$PROJECT/clone -H "X-API-Key: $KEY"
```

## 3. Watch a bug flow through the local pipeline

Open the dashboard `http://<lan-host>:8090/` in a browser, paste the API key.
Then submit a bug that describes something real in the cloned repo:

```bash
curl -s -X POST $HOST/api/v1/reports -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"title": "<real-ish bug title>", "description": "<symptoms referencing real code>",
       "reporter": "smoke-test", "project_id": "'$PROJECT'", "severity": "medium"}'
```

**Expected on the dashboard, without refreshing:** the card appears under
*Submitted*, moves to *Triaged*, bounces through *Analyzing* (triage, then
localization), and settles in *Triaged* with a triage result and localization
candidates visible in its detail drawer. GPU activity should be visible on
the host while Ollama works.

## 4. Manual local analysis (`hold` mode)

Submit a second bug with `"analysis_mode": "hold"` — it must sit in
*Triaged* untouched (no triage result). Open its detail drawer and click
**Analyze (local)**: triage + localization run on demand.

## 5. Cloud escalation (one paid call)

On a report with a completed, fresh localization, click **Analyze (cloud)**
(two-click confirm). Expected: status moves to *Fix proposed*; the detail
drawer shows a unified-diff fix proposal; the header's token-usage counter
now includes an `anthropic/...` entry (hover it).

Also verify the guard: **Analyze (cloud)** on a report *without*
localization must toast a 409, not spend a call.

## 6. Reboot survival

Reboot the Windows box. After it comes back **without anyone logging
actions**: `curl -s $HOST/health/live` succeeds and the dashboard loads.
(Docker route: requires Docker Desktop autostart — see deploy doc §3.)

## 7. Record it

Add an entry to `docs/decision_log.md`: date, deploy option used (Docker or
NSSM), pass/fail for steps 1–6, and any deviations.
