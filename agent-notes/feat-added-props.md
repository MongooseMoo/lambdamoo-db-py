# moodb added-props

## Goal

`moodb --db NEW added-props OLD [PATTERN]`: list property definitions NEW has and OLD's object of the same number lacks, one line per definition (object, property name, perms, owner). Options follow `added`: `-i`, `-F`, `-v`, `-o/--obj`, `--existing`, `--identity`; `-c/--count` prints one line per object with its count. Assignment: `C:\Users\Q\projects\mongoose\prompts\moodb-added-props.md`. PR against `MongooseMoo/lambdamoo-db-py` `main`, not to be merged by the agent.

## State

- Rebased on `mongoosemoo/main` (`7cac94e`) on 2026-10-11.
- `added_property_definitions` in `lambdamoo_db/inspection.py`, command `added-props` in `lambdamoo_db/cli.py`, README section "Comparing property definitions", tests in `tests/test_inspection.py`.
- `--holders` ends each definition's line with `value held by N of M` (the definer and its descendants whose slot is not clear); `value_holders` in `inspection.py`.
- PR: MongooseMoo/lambdamoo-db-py#15, open, not merged.
- `uv run pytest -q`: 892 passed, 8 skipped (2026-10-11).
- Ran on the hub dumps `mongoose.db.new.2026-10-11T2238` against `…T0352` with `--existing`; the report is `C:\Users\Q\projects\mongoose\reports\hp-added-props.md` (the hub is not a git repository).

## Decisions and why

- The line does not print the value. The assignment names the fields (object number and name, property name, owner, perms), and values in the Mongoose database include keys and tokens that must not be printed. `prop` and `values` read values.
- The per-object count option is `-c/--count`, as the assignment says, where `added` uses `-l/--verbs-only` for its per-verb count.
- `added` and `added-props` share `_added_pattern`, `_left_out` and `_echo_existing_summary` in `cli.py` so `--existing` means one thing.
- Without `-o`, both dumps are parsed whole: the offset index records where objects and verb programs are, not which objects define properties.

- `--holders` was added because the assignment asked for the number of objects holding a non-clear value "with `moodb values`", and `values` cannot give it: it searches stored strings by regex and has no notion of a property name or of a clear slot. The count went into the tool, as the hub's rule 10 asks.

## Surprises

- The worktree held an untracked `uv.lock`; the repo does not track one, so it is left uncommitted.

## Next step

Review and merge of the PR is Q's. Removed definitions and changed owner/perms/value are not reported by this command; `moodb diff` has those.
