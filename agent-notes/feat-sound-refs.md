# moodb sound-refs

## Goal

`moodb sound-refs`: list every sound catalog reference a dump stores (sound ids, group ids, `$catalog_sound` waif ids, path and slice strings), each with its holder and path, so no catalog row is deleted while the game still names it. Item 14 of the sound-scripts plan; design in the hub's `reports/sound-scripts-merge-2026-11-13.md` section A2 (options `--kind`, `--ids FILE`, `--skip`, `--code`, `--summary`, `--format text|jsonl`). PR against `MongooseMoo/lambdamoo-db-py` `main`, opened by the lead, not by the implementing agent.

## State

- `walk_stored_values` in `lambdamoo_db/inspection.py` is the one value walker; `find_strings` (`values`) and `find_catalog_refs` (`sound-refs`) both read it.
- `find_catalog_refs`, `find_code_literals`, `CatalogRef` in `inspection.py`; command `sound-refs` in `cli.py`; README rows; tests in `tests/test_sound_refs.py`.
- Run on the hub dump `mongoose.db.new.2026-11-12T0055`: about 5.5 minutes (whole-dump parse, like `values`); finds `#157858.break_snd` 18455, `$thing.take_snd["group"]` 3336 and `#28994.bop_snd` 67877 and 72529.

## Decisions and why

- Sound values are recognised by the game's own grammar (`#2585:_snd_candidates` on that dump): one entry or one list of entries; an entry is INT > 0, STR, or a map with `group`/`sound`. Integers under other map keys (`volume`, `vary`) are not ids.
- Where a value is taken as a sound value: property or waif slot names ending `_snd` (any case), and `$social`'s `"sound"` / `"sounds"[form]` (`#1652:test_all_socials_populated`). `$social` and `$catalog_sound` are resolved through `#0`, never by number.
- All ids must be INT > 0. The game skips sound 0 and has no group 0; object numbers in `$catalog_sound` slots are their unset default `#-1`.
- `CatalogRef.holder` (innermost property or slot name) exists for `--summary`'s per-property count; parsing it from `where` would break on map keys holding dots.
- `--skip` applies to property names and waif slot names: the pick memory `last_sound_picks` is a waif slot (`#2585:pick_value:11`). Skipped names are counted on stderr, or in the summary.
- `--code` ignores `#N` and decimals; it reports a string literal only when it is a listed path or slice, compared as the game resolves names (see `fix-sound-refs-path-case.md`).
- Text lines start with `label()` like `values`, so they carry the `$name` and object name.

## Surprises

- `descendants` follows `children`; a synthetic test database must fill them.
- On the hub dump `last_picks` (4,144) and `sound_rows_cache` (3,930) are the largest holders; pass `--skip` for caches when checking a removal.

## Next step

The lead reviews, pushes and opens the PR; CI runs the full suite there.
