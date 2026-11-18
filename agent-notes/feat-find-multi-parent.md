# moodb find --multi-parent

## Goal

Q, 2026-11-17, planning an agent base class mixed in with a role class: I needed to know whether anything in the Mongoose database already has more than one parent, and moodb could not list them (hub rule 10: add it to the tool).

## State

`moodb find --multi-parent` lists every object with more than one parent and its parents in declared order; `-c` counts them. It takes no TEXT. `find` with neither TEXT nor `--multi-parent` is a usage error (TEXT was `required=True` before; the error now says to give one or the other). Test: `tests/test_inspection.py::test_cli_find_multi_parent_lists_objects_with_several_parents` on the existing `multi_parent_db` fixture.

## Decisions and why

- An option on `find`, not a new command: `find` already answers "which objects have X", and `--verb`/`--prop` are its modes.
- Reads `object_heads`, so the lazy index serves it without parsing properties.

## Surprises

On dump 2026-11-17T2226 the list is long: `$room` (#2700) itself has two parents, mixins are a common idiom, and several hundred unnamed `$coalescible` + `$thing` objects exist.

## Next step

Merge after CI.
