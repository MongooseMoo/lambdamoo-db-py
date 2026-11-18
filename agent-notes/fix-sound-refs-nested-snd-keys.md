# sound-refs: sound ids held outside `*_snd` properties

## Goal

`moodb sound-refs` missed sound ids the game plays, and catalog rows were deleted on its word (2026-11-16; the game then raised "cannot resolve 43875"). Make it report every stored place the game reads a sound id from, and give a way to see ids held under names no rule knows.

## State

- `_sound_value_starts` in `lambdamoo_db/inspection.py` replaces `_sound_value_start`. New starts: map keys named `*_snd` (context `snd-key`); properties, slots and keys named `sound_id`, `*_sound_id`, `login_music_id` (`sound-id`); those named `sounds`/`*_sounds`, directly or one map level down by name (`sounds`).
- Strings: `$sndid, N` and `$snd,PATH` markers (context `marker`).
- `--bare` (with `--ids`): any other stored integer equal to a listed sound id (context `bare`).
- `--except-ids FILE` (with `--ids`): ids to take out of the `--ids` set. The removal's `ids.tsv` lists candidates, of which it kept the blocked ones; this gives "what was deleted" without building a third file by hand.
- Tests in `tests/test_sound_refs.py` (`nested_db`); README row updated.

## Decisions and why

- Each new name has a reader in the game, cited in the code comments and the test docstring: `#53020:storage_notice` line 7 and `#13232:sync_sound` line 12 (`electronics_profile` keys), `#4031:interaction_sound` line 8, `#10205:make_sound` line 1, `#53614:apply` line 19, `#2585:login_music_sound` line 2, `#2585:step_playback` line 23, `#29076:check` line 104, `#2585:msg` lines 3 and 5.
- Starts are tried innermost first and the first one giving a grammar position wins, so a `*_snd` slot of a waif held in a `sounds` list is still found.
- Under `sounds`, a first map key outside the sound-entry keys is a name (`interaction_sounds[line]`, `settings["sounds"]["open"]`); a key inside them makes the map an entry.
- A `$catalog_sound` waif's `sound_id` slot keeps context `catalog-waif`; that check now runs before the name rules.
- A string holding a marker is not also reported whole: the whole string is not a path and never matched an `--ids` line.
- `--bare` is a flag, not the default: small listed ids equal many unrelated numbers. It exists because name rules can only cover names someone has read; a removal check needs the rest shown.

## Surprises

- Catalog row maps kept in caches hold a `"sound_id"` key and are now reported; pass `--skip` for caches.
- A whole-dump run with a 170,000-line ids file takes about as long as before (the parse dominates).

## Next step

The lead reviews, pushes and opens the PR; CI runs the full suite there.
