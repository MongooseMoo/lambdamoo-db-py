# moodb: arguments as typed on Windows

## Goal

`moodb` arguments are patterns and MOO references, never local file names. click expands glob patterns in arguments against the current directory on Windows, so `moodb find --prop --glob '*quest*'` run beside `notes-2026-10-08-settlement-questions.md` searched for that file name.

## State

- PR: MongooseMoo/lambdamoo-db-py#18, "Test that moodb takes arguments as typed on Windows", against `main`.
- Rebased on `mongoosemoo/main` (`7400909`) on 2026-10-12. The PR adds `test_cli_patterns_are_not_local_file_names` in `tests/test_inspection.py` and changes no code.
- `uv run pytest -q`: 914 passed, 8 skipped (2026-10-12, Windows).

## Decisions and why

- The branch first added `LiteralArgumentsGroup` in `lambdamoo_db/cli.py`. PR #14 had already merged the same `windows_expand_args=False` override as `_LiteralArgs`, which made the rebase conflict. Main's class was kept and the branch's duplicate dropped; neither name was referenced elsewhere and the test names neither.
- The commit and PR were retitled from "moodb: take arguments as typed on Windows" because the commit no longer carries the fix.

## Surprises

- The same fix was written twice, on this branch and inside #14 (`feat/props-tree`).
- The worktree holds an untracked `uv.lock`; the repo does not track one, so it is left uncommitted.

## Next step

None after the merge of #18.
