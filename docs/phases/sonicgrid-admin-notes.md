# Phase 14: sonicgrid-admin-notes (B6)

Status: plan, round 1. Pairs with sonicgrid Phase 54 `bug-admin-notes`.

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
1. A new `bug_reports.admin_notes TEXT` column (`_SCHEMA` and `_migrate`).
2. Ingest maps an optional `adminNotes` from the poll payload into `admin_notes` on first import.
   It stays insert-once; edits arrive as an action (item 3).
3. A new action kind **`update_notes`**, with `params.adminNotes` (string, max 4,000 chars;
   empty string clears). The handler is in "Re-triage" below.
4. Notes in four prompts: triage, localization pass 1, localization pass 2, and the fix-proposal
   **user** template (the cached system prompt is unchanged). The block is omitted when the notes
   are empty, so prompts for bugs without notes are unchanged.
5. Tests (see Testing).

Out:
- Any change to the result push. Bugalizer does not echo notes back; sonicgrid already has them.
  This matters because sonicgrid's push schema is strict, and an unknown field gets `400` on
  every push.
- Automatic cloud spend. Re-triage re-runs local stages only. A new cloud fix still needs a human
  click (Propose fix), as today.
- Closing an already-open PR when notes change (see Risks).

## Technical approach

### Re-triage (`update_notes` handler)

The kind is added to `KINDS`. It is not paid and not an LLM kind for authorization: it needs no
consents and no model pin. The handler is inline, like `set_mode`/`close`/`reopen`
(`run_inline`, `sync/actions.py:389`):

1. **Unknown report** (not ingested yet): finish as **done** with the message "Not imported yet;
   the notes will arrive with the report." The notes are on the sonicgrid row, and the poll
   payload carries them at import (scope item 2). This is the "Submitted, first two minutes" case.
2. **Closed or deleted report:** refused ("Notes can't re-open a closed report; use Reopen.").
   Sonicgrid makes notes read-only in Completed, so this is only a guard.
3. **Otherwise**, in one DB transaction:
   - Write `admin_notes`.
   - If the notes are unchanged from the stored value: done, "Notes unchanged; nothing re-run."
   - Mark the report's completed `triage` and `localization` analyses as **`superseded`**. This is
     a new analysis status; rows are kept for history and are not deleted.
   - Mark any `fix_proposals` in a non-terminal review state as superseded too, using the
     existing status vocabulary if it fits, otherwise a new value. Open question O1.
   - Set the report status back to `triaged`. That is the validated, awaiting-Stage-2 status that
     `triage_eligible_reports` selects (`db.py:1023`).
   - The action finishes **done** with the message "Notes saved; re-triaging."
4. The existing worker loop then re-runs triage, then localization, automatically.
   `triage_eligible_reports` and the localization query must treat `superseded` like "no
   completed analysis". That is a query change, and the tests cover it.
   - **`hold` mode:** an explicit human save should still re-triage, the same way `analyze_local`
     ignores `analysis_mode` (`orchestrator.py:251`). So for a `hold` report, the handler calls
     `run_local_analysis` with a "force triage" flag instead of relying on the worker.
     Open question O2: is that worth the extra branch, or is waiting for `auto` acceptable?
5. The result push follows the normal flow. The fingerprint changes (new triage), the revision
   goes up, and sonicgrid's lane moves back to In triage, then Triaged. Stage moving backwards is
   allowed by sonicgrid's `upsert_bugalizer_result`: a newer revision wins and stage is not
   monotonic. Verify that in implementation and test it on the payload builder.

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

Bugalizer deploys first. The order is safe either way: an old Bugalizer refuses `update_notes`
with "Unknown action kind" (`sync/actions.py:177`), which shows in sonicgrid's timeline and
breaks nothing. An unknown `adminNotes` in the poll payload is ignored (`extra="ignore"`).

## Files (expected)

- `src/bugalizer/db.py`: column, migration, `superseded` status handling in the eligibility
  queries, and an `apply_admin_notes` transaction
- `src/bugalizer/ingest/sonicgrid.py`: `PollReport.adminNotes`, `map_report`
- `src/bugalizer/sync/actions.py`: `update_notes` kind, authorization, inline handler
- `src/bugalizer/pipeline/orchestrator.py`: force-triage flag, only if O2 says yes
- `src/bugalizer/llm/prompts.py` and the four call sites (`triage.py`, `localizer.py` ×2,
  `fix_proposer.py`)
- `docs/roadmap.md`: Phase 14 entry
- `docs/sonicgrid-e2e-run.md`: one line on notes

## Testing

- **Migration:** adds `admin_notes` to an existing DB (the `test_ingest.py:655` pattern).
- **Ingest:** `adminNotes` is mapped on first import. A re-walk with changed notes does not
  overwrite (insert-once is unchanged).
- **`update_notes`:**
  - unknown report → done, with the message
  - closed report → refused
  - unchanged notes → done, nothing superseded
  - changed notes → analyses superseded, status `triaged`, report becomes triage-eligible again
  - a `hold` report → re-triaged (if O2 = yes)
- **Prompts:** new direct tests for the four `format_*` builders, checking that notes appear when
  set and that output is byte-identical to today when they are empty.
- **Push payload:** after re-triage, the stage goes back to triaged with a higher revision.
- **Full suite:** once, on submission.

## Success criteria

1. A bug imported with notes is triaged with them in the prompt.
2. Saving new notes on a triaged bug sends it back to triage within one worker cycle. Sonicgrid
   shows it leave Triaged and return, and the new triage summary reflects the notes.
3. Propose fix after a notes change uses the notes and a fresh localization.
4. Bugs without notes behave exactly as today (prompt bytes unchanged).

## Risks and open questions

- **O1:** what to do with fix proposals and an open PR when notes change. The proposal: supersede
  unreviewed proposals and leave an open PR alone. The PR stays on GitHub, and the next Fix and
  open PR handles `pr_exists` as today. An alternative is to refuse `update_notes` once a PR is
  open.
- **O2:** force re-triage in `hold` mode (recommended: yes, since a human explicitly asked).
- **Cost:** each save re-runs local triage and localization on Ollama. That is free, but it takes
  minutes, and saving twice queues once: sonicgrid has one open action per bug and kind.
- **Race:** a save while triage is running. The handler takes the same report claim that
  `run_local_analysis` uses, and if the report is held it refuses with "Analysis in progress; save
  again when it finishes". Sonicgrid shows that in the timeline.
