# builtins-command

## Goal

`moodb builtins`: list every builtin function the dump's verb code calls, with
call sites (object, verb, line), counts and argument counts, including
`call_function("name", ...)` with a literal name and `X:bf_name(...)` calls to
the wrapper verbs on `#0`'s ancestry; `--against FILE` reports the called names
missing from a server's list. Requested for the Toast-to-Barn switch (hub report
`reports/barn-switch-builtins-2026-11-13.md` section 4, which specifies it).

## State

- API in `lambdamoo_db/inspection.py`: `BuiltinCall`, `builtin_wrappers`,
  `builtin_calls_in_code`, `builtin_calls`, `builtin_call_summary`.
- CLI `moodb builtins [NAMES...] [--against FILE] [--sites] [--format text|json]`
  in `lambdamoo_db/cli.py`.
- Tests `tests/test_builtins_cli.py` (synthetic fixtures), README section
  "Builtins the code calls" and a row in the command table.
- Local commits only; not pushed, no PR.

## Decisions and why

- A tokenizer, not a full parser: the repo has no MOO parser, and the grammar's
  rule for a builtin call (identifier then `(`) only needs the previous two
  tokens to exclude verb calls (`:`/`$`), property names (`.`), keywords and the
  labels of `while`/`fork`/`except`. Pulling in `moo_interp` would add a
  cross-repo dependency for no difference in the answer.
- Argument counts are strings (`"2"`, `"2@"`) so a `@` splice is visible rather
  than guessed.
- Wrappers are found by `db.ancestors(#0)` and any verb alias starting `bf_`;
  only `X:bf_name(` where `name` is such a wrapper counts as a wrapper call.
- Output is in name order so two runs (two dumps, two servers) compare line by line.

## Surprises

- `uv run` creates `uv.lock` in the worktree; it is left untracked, not committed.

## Next step

Push and PR when Q approves.
