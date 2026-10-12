# moodb find --verb / --prop: speed, and waif verbs

## Goal

Assignment from the hub's team lead, 2026-10-12:

1. `moodb --db DUMP find --verb NAME` took 70.9 s on the 176 MB dump `C:\Users\Q\projects\mongoose\db\mongoose.db.new.2026-10-12T0544`, where `grep -l` takes 3.4 s. Make it take about what `grep -l` takes. Time `find --prop` too and fix it if the cause is the same.
2. `find --verb sync_hum` must list a waif class verb stored as `:sync_hum` (#155369 in that dump), printed with its stored name as `verbs` prints it.
3. Output for existing cases unchanged (same lines, same order). Tests for both. PR against `MongooseMoo/lambdamoo-db-py` `main`; not to be merged by the agent; no attribution lines.

## State

- Worktree `C:\Users\Q\src\lambdamoo-db-py-find-verb`, branch `fix/find-verb-speed`, from `mongoosemoo/main` `27b52e0`.
- Cause read from the code, before any change: `find` loops `all_objects(db)` (`inspection.py:296`), which calls `db.objects.values()`; on a `LazyDatabase` that is `_LazyObjects.values` (`lazy.py:140`) → `load_all` (`lazy.py:227`) → `load()`, a full parse of the dump with every property value. `grep` instead uses `LazyDatabase.objects_with_programs` (`lazy.py:242`), which reads each record only up to its properties (`Reader.read_object_head_ng`).
- Nothing changed yet. Baseline timings being taken.

## Decisions and why

- My runs after the change use `--cache-dir` in a scratch directory: the index key covers the source of `lazy.py` and `reader.py`, and writing a new index deletes the other indexes for the same dump path, so using the default cache from this worktree would make the main checkout (in use by other sessions) rebuild its index.

## Surprises

## Next step

Record baselines (`find --verb sync_hum`; `find --verb event_say forbid_get weave`; `find --prop`), then RED tests, then the fix.
