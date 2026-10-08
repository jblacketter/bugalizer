# Phase 14: sonicgrid-admin-notes (B6)

Status: plan, round 3. Pairs with sonicgrid Phase 54 `bug-admin-notes`.

## Summary

A sonicgrid admin can write notes on a bug report: one free-text field, overwritten on each
save. Bugalizer must use those notes in local triage, in localization, and in the cloud fix
prompt. **Saving notes sends the bug back through triage automatically.** The automatic flow
stays the default: new bugs still triage on their own. The notes are a human's chance to add
detail, and the pipeline then re-runs with them.

Greg's ruling (2026-10-07): keep the automatic flow; notes are editable in every lane except
Completed; once a human saves notes, the bug goes back through triage with the update.

## Why the existing paths are not enough

- **Insert-once ingest.** `db.ingest_commit` inserts with `ON CONFLICT DO NOTHING`
  (`db.py:1936-1942`). The poll cursor is forward-only on `created_at`. Notes added after the
  first import never arrive through ingest.
- **Short window.** Ingest runs every 120 s (`config.py:109`) and auto triage runs right after.
  A bug is in Submitted for about 2 minutes, so most notes are written after triage.
- **No re-triage.** `analyze_local` skips triage when a completed triage exists
  (`orchestrator.py:267-276`). Auto triage skips any report with a completed triage
  (`db.py:1064`).

## Scope

In:
1. **Columns.**
   - `bug_reports.admin_notes TEXT` and `bug_reports.admin_notes_version INTEGER NOT NULL DEFAULT 0`.
   - `analyses.notes_version INTEGER`: the notes version a triage or localization run used.
   - A small table `pending_admin_notes(project_id, external_id, notes, version)` for notes that
     arrive before the report is imported.
   - All of these go in `_SCHEMA` and `_migrate`.
2. **Ingest** maps the optional `adminNotes` / `adminNotesVersion` from the poll payload on first
   import. It takes the higher version of the payload and any `pending_admin_notes` row, then
   deletes the pending row. Everything else stays insert-once.
3. **A new action kind `update_notes`** with `params: { adminNotes, notesVersion }`. See
   "Delivery protocol".
4. **The notes-stale rule**, which re-triages and re-localizes. See "Delivery protocol".
5. **Notes in four prompts:** triage, localization pass 1, localization pass 2, and the
   fix-proposal **user** template (the cached system prompt is unchanged). The block is omitted
   when the notes are empty, so prompts for bugs without notes are byte-identical to today.
6. **Push `admin.triageNotesVersion`** (round 3). This is the `notes_version` of the latest
   completed, non-superseded triage, or null. It goes in `sync/results.py` `build_payload`, next to
   `triageModel`. It gives sonicgrid authoritative evidence that triage used a given version,
   which the delivery line in Phase 54 shows. `fingerprint()` covers `admin`, so a new version
   re-pushes.
7. Tests (see Testing).

Out:
- Echoing the notes text back. Only the version is pushed (scope 6). sonicgrid's push schema is
  strict: an unknown field gets `400` on every push, so scope 6 depends on the rollout order.
- Automatic cloud spend. Re-triage re-runs local stages only. A new cloud fix still needs a human
  click (Propose fix), as today.
- Closing an already-open PR when notes change (O1 below).

## Technical approach

### Delivery protocol (round 2, after Codex's sonicgrid round 1)

Round 1 did the re-triage inside the action. It refused when the report was busy, and it left
`hold` open. Codex showed three ways that loses a save:

- sonicgrid rewriting pending params after Bugalizer has reserved them
- a save made while an action is claimed
- a save made while an analysis is running

Round 2 splits **delivery** from **re-triage**, and adds a version to both.

**Delivery** is the `update_notes` action. sonicgrid inserts one action per save, with
**immutable** params and a per-bug `notesVersion` that increases under a row lock (sonicgrid
Phase 54). Several such actions may be open for one bug. The ledger reserves the params from the
list call and dispatches those (`triage_sync._handle` / `_claim_and_start`). Because params never
change after insert, what Bugalizer reserves is exactly the save it claims.

The handler runs inline (like `set_mode`, `close` and `reopen`), in one DB transaction, and never
touches the pipeline:

1. **Malformed params:** notes not a string of at most 4,000 chars, or a version that isn't an
   integer of 1 or more → **refused**.
2. **Report not imported yet** → upsert `pending_admin_notes`, keeping the higher version →
   **done**: "Not imported yet; notes vN held for import."
3. **Closed or deleted report** → **refused** ("use Reopen"). sonicgrid's Completed guard makes
   this a backstop only.
4. **`notesVersion` not above the stored version** → **done**: "Already have notes vM; nothing
   to do." This makes stale and replayed deliveries no-ops, in either arrival order.
5. **Otherwise** → write `admin_notes` and `admin_notes_version` → **done**: "Notes vN saved;
   re-triage queued."

The handler takes no report claim and changes no status, so it can't collide with a running
triage, localization or fix. It is a column write.

**Re-triage** is pulled by the worker. A report is **notes-stale** when its
`admin_notes_version` is above the `notes_version` of its latest completed, non-superseded
triage (none counts as 0). On each loop, before Stage 2, the worker runs a reset for every
notes-stale report that is **not** in a transient claim status (`submitted`, `validating`,
`analyzing`, `fix_proposing`) and not closed or deleted. The reset is one compare-and-set
transaction on the status, like the existing claims:

- Mark the completed `triage` and `localization` analyses **`superseded`**. This is a new
  analysis status. The rows are kept.
- Mark unreviewed fix proposals superseded (O1).
- Set the status to `triaged`.

The existing Stage 2 and localization then run with the notes, and stamp `notes_version` with the
version they read from the same report row they used.

The consequences:

- **A save during a running triage or localization:** that run finishes with the old version
  and is immediately notes-stale. The next loop resets the report and runs again with the latest
  notes.
- **A save during `fix_proposing`:** the report is skipped until the fix finishes. Then it's
  stale, the fresh proposal is superseded (O1) and the report re-triages. A paid fix can be
  thrown away this way. That's the price of "saving re-triages", and the timeline shows it.
- **`hold` (O2, settled: yes).** A human save is an explicit request, the same way
  `analyze_local` ignores the mode (`orchestrator.py:251`). Triage and localization eligibility
  for `hold` reports becomes: eligible only when `admin_notes_version` is above the highest
  `notes_version` of any completed triage (or localization), **including** superseded ones.
  Without notes, `hold` behaves exactly as today. After one notes-driven run it's quiet again.
- **Multiple saves:** each version is applied in turn, or skipped when a newer one has already
  arrived. Several quick saves coalesce: only the newest version's re-triage matters, and older
  in-flight runs go stale.

The **result push** follows the normal flow. The fingerprint changes when triage is superseded
and redone, and sonicgrid's lane moves back to In triage, then Triaged. Implementation checks
that sonicgrid's `upsert_bugalizer_result` accepts a stage going backwards with a higher revision
(a newer revision wins, and stage is not monotonic), and the payload builder is tested for it.

### Prompts (`llm/prompts.py`)

The same block goes in all four templates, after the reporter's fields:

```
Notes from the project team (added after the report; treat as more reliable than the report
where they conflict):
<notes>
```

`format_*` gains an `admin_notes` argument and passes it from the report dict. Notes are clipped
to 4,000 chars. They are admin-only input, but they are still delimited as data, not
instructions.

## Rollout

**The order is a requirement** (round 3, Codex's sonicgrid round 2). It matches Phase 54's
rollout:

1. **sonicgrid deploys first**, with `BUGALIZER_NOTES_ENABLED` off. Its push schema then accepts
   `admin.triageNotesVersion`, and the poll serves `adminNotes`/`adminNotesVersion`. This must
   come first: otherwise this phase's push gets `400` on every bug and board sync stops (the same
   constraint as Phase 13).
2. **Then deploy this phase to BOWIE** and verify it with `check-service.ps1`, which must show
   `VERIFIED` at a revision that contains Phase 14.
3. **Then sonicgrid turns on `BUGALIZER_NOTES_ENABLED`.** Until then no `update_notes` is ever
   queued, so an old Bugalizer never refuses one. If one is refused anyway, sonicgrid's **Resend**
   re-delivers the current version. A refused delivery stored nothing here, so the version is
   still newer than what Bugalizer has and is applied.

## Files (expected)

- `src/bugalizer/db.py`:
  - the columns, the `pending_admin_notes` table and the migration
  - the `superseded` analysis status
  - notes-stale selection and the reset compare-and-set
  - the `hold` eligibility rule
  - `apply_admin_notes`
- `src/bugalizer/ingest/sonicgrid.py`: `PollReport.adminNotes` / `adminNotesVersion`,
  `map_report`, and the pending-merge in `ingest_commit`
- `src/bugalizer/sync/actions.py`: the `update_notes` kind, its param checks and the inline handler
- `src/bugalizer/queue/worker.py`: the notes-stale reset step, before Stage 2
- `src/bugalizer/pipeline/triage.py`, `localizer.py`: stamp `analyses.notes_version`
- `src/bugalizer/sync/results.py`: `admin.triageNotesVersion`
- `src/bugalizer/llm/prompts.py` and the four call sites (`triage.py`, `localizer.py` ×2,
  `fix_proposer.py`)
- `docs/roadmap.md`: the Phase 14 entry
- `docs/sonicgrid-e2e-run.md`: one line on notes

## Testing

- **Migration:** adds the columns and the table to an existing DB (the `test_ingest.py:655`
  pattern).
- **Ingest:**
  - `adminNotes` / `adminNotesVersion` are mapped on first import.
  - A pending row with a higher version wins over the payload, and is deleted afterwards.
  - A re-walk with changed notes does not overwrite (insert-once is unchanged).
- **`update_notes` handler:**
  - malformed params → refused
  - unknown report → pending row → applied at import
  - closed report → refused
  - version ≤ stored → done with no write, tested in both arrival orders (v2 then v1, and v1
    then v2)
  - newer version → written, with no status change and no claim taken
- **The ledger race** (Codex finding 1), deterministic:
  1. List and reserve action v1.
  2. sonicgrid inserts v2 as a separate action.
  3. Claim and dispatch v1. It applies v1.
  4. The next tick lists v2 and applies it.
  5. The final stored version is 2 and the notes equal v2's.
- **Eventual re-triage** (Codex finding 2):
  - a save while `analyzing` → that run stamps the old version → the next loop resets and
    re-triages → the final triage has the latest version
  - the same while `fix_proposing` → the reset waits until the fix ends → the proposal is
    superseded → re-triage
  - a `hold` report with a newer notes version → re-triaged and re-localized once, then quiet
  - a `hold` report without notes → never triaged (unchanged)
- **Prompts:** new direct tests for the four `format_*` builders, checking that notes appear when
  set and that output is byte-identical to today when they are empty.
- **Push payload:**
  - after a reset and re-triage, the stage goes back to triaged with a higher revision
  - `admin.triageNotesVersion` is null before any notes, equals the version the latest
    non-superseded triage stamped, and a superseded triage doesn't count
  - a new version changes the fingerprint
- **Full suite:** once, on submission.

## Success criteria

1. A bug imported with notes is triaged with them in the prompt.
2. Every save sonicgrid accepted is eventually applied without another human action. The latest
   triage's `notes_version` equals sonicgrid's `admin_notes_version`. This holds for saves made
   during a claimed delivery, a running triage or localization, a running fix, and in `hold`.
3. Saving new notes on a triaged bug sends it back to triage within one worker cycle after the
   delivery. sonicgrid shows it leave Triaged and return.
4. Propose fix after a notes change uses the notes and a fresh localization.
5. Bugs without notes behave exactly as today (prompt bytes unchanged; `hold` unchanged).

## Risks and decisions

- **O1 (proposed; Codex or Greg to confirm):** on a notes reset, supersede proposals that haven't
  been reviewed or approved, and leave an open PR alone. The PR stays on GitHub, and the next Fix
  and open PR handles `pr_exists` as today.
- **O2 (settled):** a notes save re-triages `hold` reports too (see Delivery protocol).
- **Cost:** each version re-runs local triage and localization on Ollama. That's free but takes
  minutes. A save that lands during a running fix throws that fix away once it finishes.
- **Ordering:** "higher version wins" assumes sonicgrid's version increases monotonically per
  bug. Phase 54 enforces that with a row lock inside its save function.
