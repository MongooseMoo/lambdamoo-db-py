# moodb sound-refs --output

## Goal

`moodb sound-refs --output FILE`: the command writes its own standard output to a file, because shell redirects are blocked on the machine that runs it and the listing has to be copied to the server. Without the option nothing changes.

## State

- `sound_refs` in `lambdamoo_db/cli.py` builds its lines in a list (`_sound_refs_summary` returns its lines instead of echoing them). With `--output` the list is written to the file as UTF-8 with `\n` endings and standard output gets one line, `N lines: FILE`; without it each line is echoed as before.
- Every form goes through the same list: text, `--format jsonl`, `--summary`, `--ids`.
- `skipped NAME N` lines stay on stderr and the `--ids` exit status (1 when a listed reference is found) is the same with and without `--output`.
- README command row updated. Test `test_cli_output_writes_the_lines_to_a_file_and_reports_the_count` in `tests/test_sound_refs.py` failed before the change (`No such option '--output'`) and passes after; the file's 8 tests pass.
- Committed on `feat/sound-refs-output`. The lead pushes and opens the PR.

## Decisions and why

- No shared writer existed to reuse: the only output options in `moodb` are `--output-dir` on `diff` and `history`, which write bundles of several files through their own writers. The file is written the way those writers write text (`Path.write_text(..., encoding="utf-8", newline="\n")`), and the stdout line follows `diff --plain --output-dir` in naming the destination.
- An existing FILE is replaced. `diff --output-dir` refuses an existing directory; a single listing that is regenerated from each fresh dump is rewritten in place instead, and the help text says so.
- The file is written even when there are no lines (empty file, `0 lines: FILE`), so a caller copying the file always has one.

## Surprises

None.

## Next step

Lead pushes the branch and opens the PR against `MongooseMoo/lambdamoo-db-py` `main`; CI runs the full suite there (only `tests/test_sound_refs.py` was run locally).
