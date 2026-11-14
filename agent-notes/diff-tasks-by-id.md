# diff-tasks-by-id

## Goal

For the Barn switch round-trip check (hub report `reports/barn-switch-way-back-2026-11-13.md`, F7):
`moodb diff` must (a) pair queued/suspended/interrupted tasks by task id, (b) name a
difference that is only a trailing carriage return on a string, or leading/trailing
whitespace in an object or verb name, and (c) report VM-private frame fields apart from
what the task is. Nothing else. No push, no PR (lead's instruction for this stage).

## State

Implemented with behavioural tests in `tests/test_diff_tasks_by_id.py` (passing) and a
README section "Comparing saved tasks across servers". Committed locally only.

## Decisions and why

- Before: `run()` sent each task list through one `value()` call, which pairs list
  elements by position (`compare.py` `value`, list branch). Now `tasks(field)` pairs by id;
  repeated ids on one side are a reported error and that list falls back to positions.
- Task order differences become one `serialization`/`order_changed` event (`task_order`):
  the order is how the dump was written, not what a task is.
- VM-private fields (`VM_PRIVATE_FIELDS` in `compare.py`) come from ToastStunt's
  `write_activ`/`write_activ_as_pi` (`src/execute.cc`, `mongoose` branch): operand stack,
  pc/bi_func_pc/error_pc, `a.temp`, builtin continuation, language version, VM header
  bookkeeping, and constant filler (-111, -7..-10, "No" "More" "Parse" "Infos").
- Separation is a `part` label on each event plus footer `runtime_changes`, not a new
  section: a new section would escape the redaction default that conceals `runtime`.
- `task_pairing` footer counts (matched / old_only / new_only per list) so a run can be
  judged without reading events.
- Trailing CR was invisible because the reader stripped one `\r` before every LF in any
  dump. It now strips it only in a CRLF dump, and the line ending is judged from the
  version line (a value ending in `\r` near the top no longer makes an LF dump "CRLF").
  This also makes moodb write such a value back unchanged.
- In the `world` view verbs are keyed with the name stripped, so `"eval "` vs `"eval"` is
  one verb with a labelled name change, not a removed and an added verb.

## Surprises

- `moodb diff --view checkpoint` on two real Mongoose dumps takes more than 10 minutes.

## Next step

Lead decides on push/PR. The whole suite was not run (lead: only the new test file).
