# feat/effective-census

## Goal

Count a whole family by inherited (effective) property value in one walk of the dump, for the Mongoose
"forms for everything" census: which `$gpo` descendants resolve a `construction_form`, which do not,
grouped by nearest `$`-named ancestor, optionally only things located in a `$spatial_room`.

## State

`props REF -r --effective` with `--skip-under`, `--located-in`, `--truth`, `--by-ancestor`, `--show`,
`--examples`. Tests: `tests/test_effective_census.py` (hand-built family, hand-written counts), passing
locally. README `props` row updated. Not pushed; the launcher pushes and opens the PR.

## Decisions and why

- Extended `props -r` instead of a new command: `-r --group` already groups descendants by value; the
  missing piece was resolving inherited values.
- Inheritance is resolved by the existing `inspection.lookup_property`, given an optional memo so each
  clear chain is followed once per walk. No second resolver. `relationship_graph._effective` is a separate
  older resolver bound to the graph's slot layouts; unifying the two is outside this change.
- `--located-in` follows `location` up the chain (holders however deep), so a carried item counts as in the
  room its carrier is in.
- `--by-ancestor` counts the object itself as its own nearest registered ancestor when it has a `$name`.

## Surprises

- On the 2026-11-22T0632 Mongoose dump (~36,000 things after skipping rooms, players and forms) one run takes
  about 150 s, most of it loading object records through the lazy index.

## Next step

PR review and CI.
