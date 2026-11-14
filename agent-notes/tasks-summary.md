# tasks --summary

## Goal

`moodb tasks --summary`: counts of the tasks saved in a dump by kind (queued,
suspended, interrupted) and by the verb each task is in. Wanted for the
Toast-to-Barn switch: suspended and interrupted tasks are what a Barn-written
checkpoint cannot yet hand back to Toast, so we need to see which a dump holds.

## State

Done on branch `tasks-summary`, local commits only (no push, no PR).

- API: `inspection.task_summary(infos) -> TaskSummary(kinds, verbs)`, over the
  existing `inspection.tasks(db)` records.
- CLI: `moodb tasks --summary`; composes with the existing `--this`.
- Tests: `tests/test_tasks_summary.py` (toastcore.db plus synthetic suspended
  and interrupted tasks written back as a dump).
- README: the `tasks` row in the command table.

## Decisions and why

- The verb is the innermost frame as `#DEFINER:verb`, the same thing the plain
  `tasks` listing prints, so a summary row can be found again in the listing.
- An interrupted task's status (`interrupted (<status>)`) is dropped from the
  kind count: the request was three kinds. All three are always printed, zero
  included.
- Each verb row shows its total and its count per kind (lead's request, so the
  suspended and interrupted tasks' verbs are read from the summary itself);
  `TaskSummary.verbs` holds a per-kind dict for each verb.
- `--summary` filters by `--this` first, since it reuses the listing's
  selection; no other option added.

## Surprises

- A synthetic VM must set `top` to the innermost frame's index; the reader
  reads `top + 1` activations.
- Activation `this`/`vloc` must be plain ints for the writer's header template.
- On the 2026-11-12 Mongoose dump the first run took about 156 s (index build).

## Next step

Push and PR when the lead/Q asks.
