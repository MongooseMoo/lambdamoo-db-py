# LambdaMOO database reader and exporter

Fill me in!

## cli commands
- `moodb2flat DBFile Directory`: export every object to files.
- `moodb-split DBFile Directory` / `moodb-join Directory [OutFile]`: split a v17
  dump into per-object pieces and rejoin them byte for byte.
- `moodb --db DBFile COMMAND`: read-only inspection, described below.

## Inspecting a dump with `moodb`

`moodb` answers questions about a textdump without starting a server: what an
object is, what a verb says, what a property holds, and where something is used.

```sh
export MOODB=world.db          # or pass --db world.db to every command
moodb info                     # format, object/verb/player/task counts
moodb obj '$httpd'             # header, flags, ancestry, verbs, own properties
moodb code '$httpd:GET'        # verb source, found through inheritance
moodb prop '$httpd.port'       # effective value, following clear
moodb grep -C 2 'notify('      # search all verb code
moodb refs '$httpd'            # who points at #N, in properties and code
moodb values '\.ogg$'          # where a string is stored, through lists, maps and waifs
```

### References

| Form | Meaning |
|---|---|
| `#20`, `20` | object number |
| `$string_utils` | the object in `#0.string_utils` (case-insensitive) |
| `$namespace.member`, `#20.owner` | follow object-valued properties; each intermediate value must refer to an existing object |
| `REF:NAME` | verb matched the way the server matches calls (`*` abbreviations, aliases) |
| `REF:5` | the verb at 0-based index 5 on REF itself, the order in the dump and in `verbs` output |
| `REF.NAME` | property, case-insensitive, including builtins such as `name`, `owner`, `wizard` |

Quote references in the shell: `$name` would otherwise be expanded.

Object property chains work with every command that accepts an object reference:

```sh
moodb obj '$namespace.member'
moodb children -r '$namespace.member'
moodb code '$namespace.member:look_self'
moodb prop '$namespace.member.name'
```

For `prop`, an existing literal property name wins at each object, including
names containing dots. If the full remaining name is absent, its first segment
selects the next object; the rest is resolved there. This preserves queries
such as `#20.field.with.dot` while supporting `$namespace.member.name`.
If both a literal dotted name and a traversal exist, read the target object's
number with `obj` and use that number to select the traversal unambiguously.
For `code`, the colon separates the complete object
reference from the verb name (use `::name` for an explicitly named waif verb).
Traversal reads effective inherited properties, including `clear`, just like
ordinary property queries. A scalar, missing property or recycled object stops
the traversal with a lookup error; no expressions or verbs are evaluated.

### Commands

| Command | Shows |
|---|---|
| `info` | dump format and counts of objects, verbs, players, `$names`, tasks and waifs |
| `obj REF` | owner, location, flags, parents, ancestry, verbs and own properties |
| `props REF [--builtin] [--full]` | every property, own and inherited, grouped by the defining object, with the effective value and where a `clear` value comes from |
| `verbs REF [-i]` | verbs with args, perms and owner; `-i` adds every ancestor's verbs |
| `code SPEC... [-n]` | verb source as `@program` blocks; a bare REF prints all of its verbs; `-n` numbers lines |
| `prop SPEC... [--full]` | effective property values as MOO literals, naming the definer when inherited |
| `grep PATTERN [-o REF] [-i] [-F] [-l] [-C N]` | Python regex over all verb code; `-o` limits to objects, `-l` lists verbs once, `-C` adds context |
| `find TEXT` | objects whose name or `$name` contains TEXT |
| `find --verb NAME` | objects defining a verb that a call to NAME would match |
| `find --prop NAME` | objects defining a property called NAME |
| `refs REF` | property values holding the object, and verb code lines mentioning `#N` or its `$name` |
| `values PATTERN [-i] [-F] [--in-waif REF]` | Python regex over every stored string: each object's own property values (anonymous objects included), through lists, map keys and values, and into waifs. Prints the path, e.g. `#4982.systems[10]<waif #4238>.sounds["engage"]<waif #3010>.snd = "..."`, with waif slots named and list indexes 1-based. `--in-waif` keeps only strings inside a waif of that class or a descendant |
| `children REF [-r]`, `contents REF [-r]` | direct or recursive children / contents |
| `players` | player objects and their flags |
| `tasks [-v]` | queued, suspended and interrupted tasks: id, due time (UTC) and frame; `-v` shows the whole stack |

Every command exits 1 with a message when a reference does not resolve.

### Lookup rules

Lookups follow ToastStunt rather than a simplified model:

- Ancestors are ordered like `db_ancestors()`: depth-first through parents
  in declared order, each object once. This is the order of verb lookup and of
  inherited property slots, including on multi-parent objects.
- Verb names match like `verbcasecmp()`. The x bit is not required, so command
  verbs are found too. Waif verbs (stored as `:name`) are found by their plain
  name when nothing else matches.
- A `clear` property takes its value from the first parent that inherits the
  defining object, repeatedly, as `db_find_property()` does.

### Cache

Parsing a large dump is slow, so the parsed dump is pickled under
`lambdamoo-db` in `$XDG_CACHE_HOME`, `%LOCALAPPDATA%` or `~/.cache`. Change
it with `--cache-dir`, or bypass it with `--no-cache`. The key covers the
dump's path, size and mtime, the Python version and the parser's source, so a
new dump or a parser change is reparsed. Writing a new entry deletes the stale
ones for the same path, so the cache holds one pickle per dump path.

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
