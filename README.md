# LambdaMOO database reader and exporter

Fill me in!

## cli commands
`moodb2flat DBFile Directory`

## Find object references in properties

```python
from lambdamoo_db.reader import load
from lambdamoo_db.references import find_property_references

db = load("world.db")
for path in find_property_references(db, 123):
    print(path)
# #0.properties[2].value[0].entries[1].key
```

This read-only iterator searches property values on every loaded object,
including anonymous objects, through nested lists and map keys/values.
Paths use zero-based property/list indexes and zero-based map entry indexes
in insertion order; `.key` and `.value` identify the side of each entry.
It matches `ObjNum(123)`, not integers, strings, or anonymous references.
It does not search metadata, verb source, tasks, or referenced waif bodies.
Do not mutate the database while iterating. Cyclic Python containers raise
`ValueError`; deeply nested acyclic containers use iterative traversal.

`ObjNum`, `Anon`, `MooError`, `MooCatch`, and `MooFinally` are immutable typed
values, not `int` subclasses. Use `int(value)` when a numeric ID is needed.
This preserves distinct object, integer, and error keys in loaded MOO maps.
The JSON exporter retains its existing numeric representation and is not a
lossless representation of typed map keys.
