# sound-refs: match paths and slices as the game resolves them

## Goal

`moodb sound-refs --ids FILE` guards deletions from the sound catalog. It compared path and slice strings exactly, while the game resolves a stored string more leniently, so a stored name differing only in case from a listed one was not reported. Make the comparison follow the game.

## State

- `catalog_name_key` in `lambdamoo_db/inspection.py`: URL-decode, lowercase, drop leading zeros of a trailing `#N`.
- The `--ids` filter in `cli.py` (`sound_refs`) and `find_code_literals` (`--code`) compare strings by that key. Printed values are the stored strings.
- Help text and README row updated. Test `test_cli_ids_match_paths_and_slices_as_the_game_resolves_them` in `tests/test_sound_refs.py` failed before the change and passes after.
- Committed on `fix/sound-refs-path-case`. The lead pushes and opens the PR.

## Decisions and why

- The game is `#6128:resolve` (read in the hub dump `mongoose.db.new.2026-11-14T2214`): the asset lookup is `af.asset_location = ?` for the string and its `url_decode`, case-sensitive (the column has no collation, and the SQL names none); when that finds nothing it looks up `Sounds.display_name` for the lowercased string, trying a slice number as written, two-digit and unpadded. A whole-file sound's display name is its asset path and catalog names are lowercase, so a path differing only in case resolves through that second lookup.
- Backslashes, a leading slash and a missing extension are not resolved by the game, so they are not normalised here.
- The key ignores all leading zeros of a slice number. The game tries only two-digit and unpadded, so this reports slightly more than the game resolves (stored `#1` against a three-digit `#001`); for a deletion guard that is the safe side.
- `AssetFiles.path_key` (sound-scripts `naming.py:path_key_for`: relative path without extension, lowercased) is not read by `resolve`, so it plays no part.
- No new flag: there is no use for exact matching in a deletion guard.

## Surprises

- sound-scripts' schema has an index for `asset_location = ? COLLATE NOCASE`, but `#6128:resolve` does not use that collation; case-insensitivity comes only from the lowercased display-name lookup.

## Unverified

- The server's `url_decode` was not read; `urllib.parse.unquote` decodes `%XX` only and leaves `+` alone.

## Next step

The lead reviews, pushes and opens the PR; CI runs the full suite there.
