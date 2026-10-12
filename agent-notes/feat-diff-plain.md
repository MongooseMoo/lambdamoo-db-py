# feat/diff-plain: `moodb diff OLD --plain`

## Goal

A plain source-reading output of the verb changes between two dumps, to replace
reading `diff --view code --output-dir` bundles (Markdown- and HTML-escaped,
JSON evidence repeated, long added verbs cut off). Asked for by the Mongoose
hub's team lead on 2026-10-11, who has about 2,700 verb changes to read.

## State

- Worktree `C:\Users\Q\src\lambdamoo-db-py-diff-plain`, branch `feat/diff-plain`
  off `mongoosemoo/main` `14b74cf`. The repo has no `master`; `main` is the
  default branch.
- Written: `lambdamoo_db/verb_changes.py`, the `--plain`, `--exclude`,
  `--existing`, `--identity`, `--full` options on `diff` in `cli.py`,
  `tests/test_verb_changes.py` (9 tests, passing), README section "Reading the
  verb changes as source".
- Whole suite on 2026-10-11: `901 passed, 8 skipped in 100.81s` (the 8 are the
  roundtrip tests that need external fixtures).
- Run on the hub's dumps `2026-10-09T2320` -> `2026-10-11T1750`, both already
  indexed: all objects 46 s, 576 objects, 2880 added, 664 changed (3 only in
  the Last modified line), 691 removed verbs; with `--existing` 39 s, 404
  objects, 794 added, 664 changed, 690 removed, and 172 objects left out
  (2086 added, 1 removed), 31 kept on number alone. `-o '#1'` takes under a
  second. No `&quot;`, `&amp;` or "incomplete preview" in any file written.
- Committed and pushed; PR opened against `MongooseMoo/lambdamoo-db-py` `main`.
  Not merged: the team lead reviews and merges.

## Decisions and why

- A flag on `diff` (`--plain`), not a new `--view`: `--view` is shared with
  `history` and `ReportOptions`, and the plain output is not a report.
- `--plain` reads through the offset index like `added` and does not run
  `compare_snapshots`: it needs only verbs, and the full comparison takes
  minutes and gigabytes. It therefore refuses `--format`, `--section`,
  `--kind`, `--max-events`, `--stop-after`, `--redact`.
- Pairing is `--view code`'s (`verb_signature` plus occurrence), then two more
  passes over what is left unpaired on an object: same source, then same
  names. That is how a rename or a new argument spec becomes one CHANGED verb
  with a `names:` or `args:` line, which the request asked for; signature
  pairing alone can never produce those.
- The "Last modified" line: `--view code` compares `av.code != bv.code`
  exactly (`compare.py`, `verbs`), so it does count a trailer-only change. The
  plain view keeps that and labels such verbs.
- `verb_holders` reads the head of every record the program index does not
  list, so a verb defined and never programmed is still compared. It lives in
  `verb_changes.py`, not `lazy.py`, because `lazy.py` is part of the index key
  and editing it would make every cached index rebuild.
- `--existing` keeps an object NEW no longer has: it was OLD's.

## Surprises

- The request said `origin/master`; `origin` is `ctoth/lambdamoo-db-py` and the
  branch is `main`. The PR goes to `MongooseMoo/lambdamoo-db-py` `main`.

## Next step

Answer review comments on the PR. After it is merged, remove this worktree and
the `feat/diff-plain` branch. Not checked: the 2,700-verb output was read for
#1 only, and the totals were not reconciled verb by verb against the
`--view code` bundle (its 2769 "selected changes" counts events, not verbs).
