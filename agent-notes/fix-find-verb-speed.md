# moodb find --verb / --prop: speed, and waif verbs

## Goal

Assignment from the hub's team lead, 2026-10-12:

1. `moodb --db DUMP find --verb NAME` took 70.9 s on the 176 MB dump `C:\Users\Q\projects\mongoose\db\mongoose.db.new.2026-10-12T0544`, where `grep -l` takes 3.4 s. Make it take about what `grep -l` takes. Time `find --prop` too and fix it if the cause is the same.
2. `find --verb sync_hum` must list a waif class verb stored as `:sync_hum` (#155369 in that dump), printed with its stored name as `verbs` prints it.
3. Output for existing cases unchanged (same lines, same order). Tests for both. PR against `MongooseMoo/lambdamoo-db-py` `main`; not to be merged by the agent; no attribution lines.

## State

- Worktree `C:\Users\Q\src\lambdamoo-db-py-find-verb`, branch `fix/find-verb-speed`, from `mongoosemoo/main` `27b52e0`.
- Cause read from the code, before any change: `find` loops `all_objects(db)` (`inspection.py:296`), which calls `db.objects.values()`; on a `LazyDatabase` that is `_LazyObjects.values` (`lazy.py:140`) → `load_all` (`lazy.py:227`) → `load()`, a full parse of the dump with every property value. `grep` instead uses `LazyDatabase.objects_with_programs` (`lazy.py:242`), which reads each record only up to its properties (`Reader.read_object_head_ng`).
- Baselines before the change, this worktree at `27b52e0`, default index cache: `find --verb sync_hum` 73.3 s (five `"*"` verbs, nothing on #155369); `find --prop hum_sound sync_hum 'forbid_*' --glob` 74.5 s (run while another full parse was running).
- RED seen: 9 new tests in `tests/test_lazy.py` failed (full parse refused; waif verb not listed).
- After the change, same dump, index in a scratch `--cache-dir`: `find --verb sync_hum` 2.9 s (2.94, 2.86), with #155369's `":sync_hum"` as a sixth line; `find --prop` (same three names) 2.9 s; `grep -l ':sync_hum'` 3.4 s in the same session. `find --verb event_say forbid_get weave` and the `find --prop` run print exactly what they printed before.
- First version of the fix opened the dump once per record and took 8.7 s; a profile showed 44,565 `open` calls costing about 4 s, so the scan now holds one handle (`LazyDatabase._scanning`).
- `uv run pytest -q`: 912 passed, 8 skipped (2026-10-12).
- PR: MongooseMoo/lambdamoo-db-py#17, open, not merged.
- The change:
  - `Reader.read_propdef_names` split out of `readProperties` (`reader.py`).
  - `LazyDatabase.read_head`, `object_heads`, `defined_property_names` (`lazy.py`); `objects_with_programs` and `verb_changes.verb_holders` use `read_head`.
  - `inspection.object_heads`, `defined_property_names`, `waif_spelling`, `answers_call`; `find_verb` uses `waif_spelling`.
  - `cli.find` uses them; README says what `find --verb`/`--prop` read.

## Decisions and why

- My runs after the change use `--cache-dir` in a scratch directory: the index key covers the source of `lazy.py` and `reader.py`, and writing a new index deletes the other indexes for the same dump path, so using the default cache from this worktree would make the main checkout (in use by other sessions) rebuild its index.

- `find --verb --glob` also matches a waif class verb's name without its colon (`sync_*` finds `:sync_hum`). The assignment names only the plain form; the glob form would otherwise disagree with it.
- `added-props` still parses both dumps whole through `all_objects`. It could use `defined_property_names` for names, but it prints perms and owner, which are with the values. Left alone: not in the assignment.

## Surprises

- The README said `find --verb` and `find --prop` "parse the whole dump each time": the slowness was the documented design, not a regression.
- `uv run` leaves an untracked `uv.lock` and `.venv` in the worktree; the repo tracks neither.
- Merging this changes `lazy.py` and `reader.py`, which are in the index key, so the first query from the main checkout after the merge rebuilds each dump's index once (a full parse).

## Next step

Review and merge of the PR is Q's. After the merge, remove this worktree and branch.
