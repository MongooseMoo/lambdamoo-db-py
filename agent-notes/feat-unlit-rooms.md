# feat/unlit-rooms

## Goal

Count the Mongoose rooms that the lighting change leaves dark: once indoor rooms no longer get light for
free, which rooms have no sky, no builder-set fill and no light source inside them.

## State

`moodb unlit [ROOT] [--verb NAME] [--list KIND] [--limit N]`. Tests: `tests/test_unlit.py` (hand-built
world, hand-written output). README command table has its row. PR opened against `MongooseMoo/main`.

## Decisions and why

- Kinds in order: `outdoor` (`.outdoor` true) beats `filled` (`.ambient_light` > 0) beats `sourced` (a
  thing with the verb anywhere in its contents) beats `dark`. Non-numeric values count as 0.
- A light source is anything with the verb, found by `inspection.find_verb` (inherited, MOO name
  matching), so the command does not decide whether the light is switched on. Closed containers are
  looked into, so `sourced` is an upper bound and `dark` a lower bound.
- The two properties are looked up on ROOT first; if ROOT lacks one, the command fails with the lookup
  error instead of reporting zero rooms.

## Surprises

- The first draft was saved with CRLF line endings, so the whole of `cli.py` showed as changed; restored to LF.

## Next step

CI and merge.
