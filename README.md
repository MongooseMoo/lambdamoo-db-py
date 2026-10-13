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
| `props REF [--builtin] [--full] [--name GLOB]... [-r [--group]]` | every property, own and inherited, grouped by the defining object, with the effective value and where a `clear` value comes from. `--name '*_msg'` keeps matching names. `-r` prints one line per value: REF's effective values, then each descendant's own (not clear) values, so `props '$thing' -r --name '*_msg'` shows which descendants override what. `--group` (with `-r`) prints one line per distinct name and value with the number of objects holding it and the first five |
| `verbs REF [-i]` | verbs with args, perms and owner; `-i` adds every ancestor's verbs |
| `code SPEC... [-n]` | verb source as `@program` blocks; a bare REF prints all of its verbs; `-n` numbers lines |
| `prop SPEC... [--full]` | effective property values as MOO literals, naming the definer when inherited |
| `grep PATTERN [-o REF] [-i] [-F] [-l] [-C N]` | Python regex over all verb code; `-o` limits to objects, `-l` lists verbs once, `-C` adds context |
| `added OLD [PATTERN] [-i] [-F] [-l]` | added or replaced verb code lines relative to OLD; optional Python regex, `-F` for literal text, `-l` for counts per verb |
| `find TEXT` | objects whose name or `$name` contains TEXT |
| `find --verb NAME` | objects defining a verb that a call to NAME would match, waif class verbs (stored as `:NAME`) included |
| `find --prop NAME` | objects defining a property called NAME |
| `refs REF` | property values holding the object, and verb code lines mentioning `#N` or its `$name` |
| `values PATTERN [-i] [-F] [--in-waif REF]` | Python regex over every stored string: each object's own property values (anonymous objects included), through lists, map keys and values, and into waifs. Prints the path, e.g. `#4982.systems[10]<waif #4238>.sounds["engage"]<waif #3010>.snd = "..."`, with waif slots named and list indexes 1-based. `--in-waif` keeps only strings inside a waif of that class or a descendant |
| `children REF [-r]`, `contents REF [-r]` | direct or recursive children / contents; `--summary [--format json]` measures the whole reachable graph |
| `players` | player objects and their flags |
| `tasks [-v]` | queued, suspended and interrupted tasks: id, due time (UTC) and frame; `-v` shows the whole stack |
| `batch [QUERY...]` | the answers to several of the commands above, in one run |

Every command exits 1 with a message when a reference does not resolve.

### Measuring contents and inheritance trees

Use summaries to measure the work a recursive traversal would reach without
printing thousands of object names or importing Python internals:

```sh
moodb --db world.db contents '#1261' --summary
moodb --db world.db contents '#1226' --summary --format json
moodb --db world.db children '$root_class' --summary --format json
moodb --db world.db batch 'contents #1261 --summary' 'contents #1226 --summary'
```

`--summary` always traverses the complete reachable graph; `-r` is unnecessary.
Text and JSON report the same fields. Summaries contain object numbers and
counts, without object names or property values. `batch` reuses the loaded
records, and prints a `=== QUERY` separator before each answer.

| Field | Meaning |
|---|---|
| `root`, `relation` | resolved object number and `contents` or `children` |
| `direct_count` | distinct existing objects directly linked from the root, excluding the root itself |
| `descendant_count` | distinct existing reachable objects, excluding the root |
| `leaf_count` | descendants with no stored references in this relation |
| `max_depth` | greatest shortest-path distance from the root; zero for an empty graph |
| `missing_reference_count` | stored edges pointing to objects absent from the dump |
| `repeated_reference_count` | stored edges to an already discovered object, including duplicate entries, shared descendants and cycles |

Traversal is iterative and visits each existing object once, even with cycles
or multiple parents. Edge diagnostics cover the root and all visited objects.
A node whose only references are missing is not a leaf. Repeated references
are not necessarily corruption: multiple inheritance can share descendants.
`--format json` requires `--summary`; ordinary listings retain their format.

### Comparing verb source

```sh
moodb --db new.db added old.db
moodb --db new.db added old.db 'notify\('
moodb --db new.db added -l -F old.db 'E_INVARG'
moodb --db new.db added -l --existing old.db 'hp_'
moodb --db new.db added -o '$room' -o '#410' old.db
moodb --db new.db added -o '$room' --verb say --verb enterfunc -v old.db 'hp_'
```

`added` pairs verbs by object number, exact names and argument specification,
then by occurrence among duplicate definitions. Inserting another verb before
an existing one does not change that pairing. Renaming a verb or changing its
argument specification makes it a new definition; extra duplicates are marked
`new verb` too. Object numbers must describe the same world in both dumps.

Within each pair, a line diff ignores leading and trailing whitespace for
alignment and prints insertions and replacements with their original text and
line numbers in the new dump. Regex filters search that original text. Moved
lines can appear as additions. Deleted lines, removed verbs and metadata
changes are not reported. Filters and the
summary count only matching added lines; identical dumps print
`0 added lines in 0 verbs`. Both dumps use the offset index when available,
or a full parse with `--no-cache`.

`-o REF` (repeatable) limits the comparison to those objects of the new dump.
`--verb NAME` (repeatable) keeps only verbs that NAME would call, by MOO name
matching, so `--verb get` finds a verb named `g*et take`. `-v` inverts PATTERN:
only added lines that do not match it. The last example asks what else changed
in two verbs besides one piece of work's own lines.

`--existing` answers "what did this work add to objects it did not create". It
leaves out an object whose number the old dump lacks, and one whose number was
recycled and reused in between: both dumps have an object there, but its
`creation_time` differs. Name another property with `--identity PROP` for a
world that records creation differently. An object with no such property on
either side is kept, since only its number says it is the same one; check
those by hand. A second summary line counts all three, for objects that had
matching lines, and names the ones kept on their number:

```text
--existing left out 20 objects OLD lacks and 54 reused numbers; kept 3 on number alone (no .creation_time to compare): #1641 #9657 #22617
```

### Comparing property definitions

```sh
moodb --db new.db added-props --existing old.db 'hp_'
moodb --db new.db added-props -c -o '$root_class' -o '#410' old.db
```

`added-props` lists the properties the new dump defines on an object that the
old dump's object of the same number does not define. A definition is a
property the object itself defines, as `obj` lists under "own properties"; an
inherited slot is not one. Names compare without regard to letter case, as the
server compares them, so a property renamed only in case is not new. One line
per definition, with its permissions and owner; values are not printed (`prop`
and `values` read those):

```text
#1 $root_class "Root Class".hp_generation  rc  owner #2
3 added property definitions on 2 objects
```

PATTERN is a Python regex on the property name, with `-i`, `-F` and `-v` as on
`added`. `-o REF` (repeatable) limits the comparison to those objects of the
new dump. `-c` prints one line per object with its count. `--holders` ends
each definition's line with `value held by N of M`: of the M objects that are
the definer or a descendant of it, N have a value of their own (a slot that is
not clear); the definer is always one of them. `--existing` and
`--identity PROP` mean what they mean on `added`, with the same second summary
line: an object the old dump lacks has every definition reported, and
`--existing` leaves it out, along with reused numbers. Removed definitions and
changes to a value, owner or permissions are not reported; `moodb diff` has
those.

Without `-o`, both dumps are parsed whole even when they have an offset index:
the index records where each object and verb program is, not which objects
define properties. With `-o`, only those objects and their ancestors are read.

### Lookup rules

Lookups follow ToastStunt rather than a simplified model:

- Ancestors are ordered like `db_ancestors()`: depth-first through parents
  in declared order, each object once. This is the order of verb lookup and of
  inherited property slots, including on multi-parent objects.
- Verb names match like `verbcasecmp()`. The x bit is not required, so command
  verbs are found too. Waif verbs (stored as `:name`) are found by their plain
  name when nothing else matches; `find --verb` lists them beside the plain
  verbs, under the stored name.
- A `clear` property takes its value from the first parent that inherits the
  defining object, repeatedly, as `db_find_property()` does.

### Several queries in one run

`batch` answers any mix of commands from one process, so objects read for one
query are reused by the next. Give each query as one quoted argument, or one
per line on standard input:

```sh
moodb batch 'obj $httpd' 'code $httpd:GET 852:12' 'prop $httpd.port'
moodb batch < queries.txt
```

Each answer follows a `=== QUERY` line. The run stops at the first query that
fails, with that query's error and exit status.

### Speed and the index

The first query on a v17 dump parses all of it once (about 25 s for a 94 MB
dump) and saves an index: the byte offset of every object record and verb
program, and every object's name. The index is about 1 MB. Later queries read
only the records they need from the dump itself:

- `obj`, `props`, `verbs`, `code`, `prop`, `children`, `contents`, `players`,
  `tasks` and `find TEXT` read a few records, or none.
- `grep` reads the verb definitions and code, not the properties.
- `find --verb` reads every object's verb definitions and `find --prop` the
  names of the properties each object defines; neither parses a property
  value or verb code.
- `info`, `refs` and `values` look at every object, so they parse the whole
  dump each time.

The index lives under `lambdamoo-db` in `$XDG_CACHE_HOME`, `%LOCALAPPDATA%` or
`~/.cache`. Change that with `--cache-dir`. `--no-cache` parses the whole dump
and neither reads nor writes an index. The key covers the dump's path, size
and mtime, the Python version and the parser's source, so a new dump or a
parser change gets a new index. Writing one deletes the stale ones for the
same path, and the parsed-dump pickle that earlier versions kept there. Dumps
older than format 17 have no index and are parsed whole.

## Diff database checkpoints

```sh
moodb --db new.db diff old.db
moodb --db new.db diff old.db --view code --object '#852'
moodb --db new.db diff old.db --view checkpoint --format jsonl
moodb --db new.db diff old.db --output-dir review-pair
moodb diff-read review-pair --object '#852' --category code --limit 50
moodb diff-read review-pair --event EVENT_ID --side old --range 1:80
```

The default `world` view compares permanent numeric object slots, definitions,
stored property values, verb metadata and exact source, players, recycled slots,
and reachable anonymous objects/waifs. `code` selects verb changes; `checkpoint`
includes raw heap numbering, runtime tasks and stored serialization fields.
Equality always refers to the selected scope. Numeric slot pairing assumes the
same world; it cannot prove an object was not recycled between snapshots.

Bundles contain `summary.md`, `objects.jsonl`, per-object Markdown, complete
typed events and content-addressed large payloads. Agents can start with the
summary and retrieve one object or payload range without opening either dump.
Source ranges use one-based half-open line bounds; string/list/map ranges use
zero-based half-open bounds. `diff-read` verifies digests, event IDs and indexes.
Both source files are frozen and hashed before comparison; diff ignores the
inspection offset cache and fully materializes the selected snapshots.

Repeat `--section` to select scope and `--kind` to filter displayed events.
Text defaults to 200 displayed events; JSON Lines and bundles default to all.
`--max-events 0` scans everything and shows only the summary. `--stop-after N`
stops comparison and reports an incomplete scan. Exit codes are 0 equal, 1
different, 2 error/incomplete, 3 unknown identity. Display filters and limits
do not hide a changed status.

`--output-dir` alone writes the bundle and prints `STATUS: bundle DIR`. Add
`--format text` or `--format jsonl` to also get that report on standard output
from the same comparison; the `STATUS: bundle DIR` line then goes to standard
error. Each keeps its own default limit and the bundle is written first, so a
refused directory fails before any report is printed.

### Reading the verb changes as source

```sh
moodb --db new.db diff old.db --plain --output-dir review-source
moodb --db new.db diff old.db --plain --existing --output-dir review-existing
moodb --db new.db diff old.db --plain -o '#1' -o '$room' --full
moodb --db new.db diff old.db --plain --exclude '$test_runner' --context 8 --output-dir review-rest
```

`--plain` writes the verb changes to be read from top to bottom, by a person
or an agent. Nothing is escaped, encoded or cut short, there is no JSON and
there are no hashes: verb source appears as the MOO code it is, once.
`--output-dir DIR` gets `index.txt` and one `objects/permanent-N.txt` per
changed object, and must not exist yet; without it the same text goes to
standard output, index first.

Each verb starts with a header: `ADDED`, `CHANGED` or `REMOVED`, the object
and the verb's first name, its 0-based position on the object (what
`code REF:N` takes), then all its names, argument spec, permissions and owner.
After the header:

- an added verb has its complete source, with line numbers;
- a removed verb has its complete old source, with line numbers;
- a changed verb has one line for each thing that changed besides the source
  (`names:`, `args:`, `perms:`, `owner:`, each as `OLD -> NEW`), then a
  unified diff of its source lines. `--context N` sets the unchanged lines
  around each change (default 3). `--full` shows the whole new verb instead,
  numbered as the new dump has it, with `+` on added lines and `-` on removed
  ones. A verb whose source is the same says `source unchanged`.

```text
========================================================================
CHANGED  #1:moveto  [12]
names "moveto"   args this none this   perms rxd   owner #2 "Wizard"
source: +2 -1 lines
========================================================================
@@ -1,4 +1,5 @@
 {where} = args;
 if (!valid(where))
-return E_INVARG;
+raise(E_INVARG);
+endif
 return this:go(where);
```

Verbs pair as in `--view code`: by exact names, argument spec and occurrence
among identical definitions, and their source lines compare exactly. Of the
verbs that leaves unpaired on an object, an old and a new one with the same
source are shown as one changed verb (a rename, or a new argument spec), and
so are two with the same names; `--view code` reports each of those as one
verb removed and one added. Anonymous objects are not compared.

The `"Last modified ..."` line a verb ends with is part of its source and is
compared like any other line, as `--view code` does, so a verb that was only
saved again counts as changed. Its header then says `only the Last modified
line changed`, and the index counts such verbs. When that line did not change
and falls outside every hunk, the header quotes it.

`-o REF` (repeatable) compares only those objects and `--exclude REF`
(repeatable) leaves objects out. `--existing` keeps only objects the old dump
already had, with the meaning it has on `added`: it leaves out a number the
old dump lacks and a number recycled and reused in between, told apart by
`--identity PROP` (default `creation_time`). An object the new dump no longer
has was the old dump's, so it stays. `index.txt` names the options in force,
then lists each object in number order with its counts of added, changed and
removed verbs, marked `[new object]`, `[removed object]`, `[reused number]` or
`[by number only]` where that applies, and then lists what `--exclude` and
`--existing` left out, with the same counts, so several runs can be checked to
cover everything between them.

`--plain` reads both dumps through the offset index, as `added` does, and
neither freezes nor hashes them. It cannot be combined with `--format`,
`--section`, `--kind`, `--max-events`, `--stop-after` or `--redact`: nothing
is redacted. It exits 0 when it wrote no verb change and 1 when it wrote any.

```sh
moodb history --repo /path/to/mongoose_db --from OLD_SHA --to NEW_SHA --output-dir review-history
moodb history --repo /path/to/mongoose_db --from OLD_SHA --to NEW_SHA --mode adjacent --output-dir review-timeline
moodb history --repo /path/to/mongoose_db --branch master --from-date 2026-01-01T00:00:00Z --to-date 2026-02-01T00:00:00Z --output-dir review-month
moodb diff-read review-timeline --edge EDGE_ID --object '#852'
```

History reads immutable local Git blobs, preferring verified `db/MANIFEST`
pieces over `mongoose.db.new`; it never checks out or fetches commits.
Endpoint mode compares just the endpoints. Adjacent mode follows first parents
and retains each transition, including reversions. The default cap is 100 edges;
`--max-edges` raises it explicitly. `--keep-going` records failed edges and keeps
exit 2. Date selection pins the declared branch once and uses committer time;
checkpoint time remains unknown without recognized backup metadata.
`--resume-from PREVIOUS_REPORT` validates and reuses matching completed edges
into a new output directory. Caught errors and user interrupts retain the
verified completed prefix with covered and requested endpoints and exit 2.
Existing destinations are refused. History and
diff-read and history-read ignore `MOODB` and reject an explicit global `--db`.

### Trace relationships through checkpoints

`history-read` interrogates an existing history bundle without opening its
source dumps or consulting Git. For a useful change timeline, build the bundle
with `history --mode adjacent`: endpoint comparisons can hide a changed and
then reverted connection, or an object removed and recreated in the same slot.

```sh
moodb history-read review-timeline --object '#852'
moodb history-read review-timeline --object '#852' --target '#410' --property systems
moodb history-read review-timeline --target '#410' --section properties --direction backward
moodb history-read review-timeline --object '#852' --category code --limit 20 --offset 20
moodb history-read review-timeline --object '#852' --format json
moodb history-read review-timeline --max-scan-events 10000 --max-scan-edges 100
```

Object selectors and targets are permanent numeric slots, not `$names` whose
meaning can change across checkpoints. `--target` answers where stored typed
object-reference evidence appeared, disappeared, or remained present while
other data changed. Each match reports old and new presence separately:
`present -> absent`, `absent -> present`, or `present -> present`. A number,
string, verb-code mention or heap ID is not a permanent object reference.
Object-valued map keys count; integer keys with the same number do not. Target
queries resolve complete externalized payloads and reachable anonymous/waif
nodes, including cyclic heaps. They report stored evidence, not a call made by
the running server. Unavailable or concealed reference evidence yields
`unknown`, including possibly matching concealed events. It cannot establish
that a reference was absent. Presence describes the changed payload or path,
not a reconstructed complete object state; a reference elsewhere on the object
may still exist.

`--property NAME` matches a stored property step on either event path, ignoring
ASCII case. Repeat `--section` and `--kind` to select events. `--path-prefix`
accepts a JSON array of exact typed steps from an event's old or new path; copy
that array from JSON output to select a precise definition or nested location.
Concealed coordinates remain possibly matching events with `selector_match:
unknown`. Lifecycle states are retained in each searched transition even when
the property/category filter selects other events. No identity is inferred
from a property-name spelling or an anonymous dump number.

Text output prints transition commits, event and edge IDs, safe property/field
locators, reference presence and coverage. It does not print property values,
source or arbitrary map-key strings. Explicit `--format json` returns stored
typed events, their complete paths and payload locators. Retrieve a payload
with `diff-read review-timeline --edge EDGE_ID --event EVENT_ID --side old
--range START:END`. Each transition keeps its own evidence: change and revert
events are never collapsed. `--direction backward` searches the latest
transitions first; within one transition event order reverses too, without
claiming an order of actions between the two checkpoints.

Pages bound returned events (`--limit`, default 50). `next_offset` tells the
caller where the next page starts; each page is a fresh query. Searches are
bounded separately by `--max-scan-events` (default 100000) and
`--max-scan-edges` (default 1000). A bound reports a lower-bound match count,
`query_scan_complete: false`, a coverage gap and exit 2. Raise the bound to
reach later pages; a display offset cannot bypass the scan cap. Successful
queries exit 0; invalid evidence exits 2. A successful query can still describe
an incomplete source history: inspect coverage before interpreting no matches.

Coverage lists failed comparisons, endpoint-only unobserved intervals,
unreached requested endpoints, omitted merge parents, unknown identity,
truncated details, filtered event kinds, selected object/section/view scope,
concealed evidence and query bounds. Complete authentication proves the report
is internally consistent with its digests and typed evidence; it is not an
external signature or proof of a complete history. Numeric slot continuity
remains `unknown`: two observed `#852` records do not prove one continuous
object incarnation. An edge union across time does not prove a route existed
at any one checkpoint, a time-respecting route, causality, or who changed the
connection. Commit metadata is checkpoint provenance, not attribution.

The API is `lambdamoo_db.history_read.query_history(path, object_id=852,
target=410, limit=50)`. It authenticates the complete history and each pair
once per query, consumes one pair at a time, caches resolved target payloads
within that pair, and keeps a bounded output page. Search caps bound event
interrogation, not mandatory authentication I/O or the size of one pair. The
existing pair validator still materializes one pair's event stream; this API
does not promise a memory cap for an arbitrarily large pair. Repeated
`diff-read` calls authenticate the history and selected pair again on every
retrieval; `history-read` avoids that multiplicative validation cost when
tracing many transitions. Dumps and benchmark/report artifacts belong outside
commits.

Reproduce the synthetic reader benchmark in a new directory outside the checkout:

```sh
uv run python benchmarks/history_read.py /path/outside/checkout/trace-benchmark --rounds 10 --edges 8 --width 40
```

It checks that both retrieval methods return the same event IDs, alternates
execution order, and reports every timing sample, median, pair-validation
count and a separate `tracemalloc` peak. That peak measures Python allocations,
not resident memory. The synthetic workload changes and reverts integer map
values; it does not establish performance on a large Mongoose history.

Optional redaction uses a declarative JSON file:

```json
{"schema_version":1,"rules":[{"selector":{"section":"properties","definer":"42","name":"api_key","occurrence":"*"},"action":"conceal"}]}
```

Pass `--redact rules.json`; `--redact-strict` makes unmatched selectors an error
(history aggregates matches across edges). Selectors cover whole sections,
properties by definer/exact name/occurrence, verbs by object/exact full
name/occurrence, waif class/slot, or object name/aliases. Concealment propagates
through heap graphs and derived labels and coalesces concealed changes before
publishing counts and IDs. Whole-property rules include waif slots, definition
rules cover matching waif class definitions, and whole-verb rules include
reachable anonymous source. Whole-section match totals record whether
each side covers an eligible container, including empty containers;
concealed owner, property and program counts stay hidden.
Runtime is concealed by default with any policy;
a runtime section rule with action `reveal_runtime` opts in. Redaction cannot
be combined with `--stop-after`.

See the [full diff and history specification](specs/full-diff.md) for pairing,
typed values, uncertainty, coverage and report integrity contracts. Reader
input that would collapse legal typed map keys fails explicitly. Numeric map
keys follow the declared Toast Num64/int32 GNU comparator policy; integer/object
keys retain signed64 raw payloads and compare by the narrowed server key identity.
Out-of-width keys, NaN keys and NUL string keys are explicitly unsupported;
ordinary scalar values retain their parsed precision. The first
release materializes both snapshots and reachable heap evidence. A large world
comparison can take minutes and several gigabytes of RAM. Start with `--view code`
for source review, or `--object '#852'` to focus a world report. Source loading
still parses the complete snapshots. Streaming and dependency-based history
skipping remain future work.

Raw `split`/`join` still preserve unsupported map bytes: their private boundary
scanner does not construct semantic dictionaries. Diff and inspection loaders
keep the strict fidelity checks.

## Compare loaded databases

```python
from lambdamoo_db.compare import compare_databases
from lambdamoo_db.reader import load

result = compare_databases(load("old.db"), load("new.db"))
print(result.report())
```

The structural comparison includes saved queued, suspended and interrupted
tasks, their VM/activation fields and runtime values, clocks, connection
records, header counts and stored format metadata. Object and verb records,
waifs and pending finalization values are compared too. Existing float
tolerances still apply; this is not a byte comparison.

Property lists compare by stored slot position, including each slot's name,
value, owner and permissions. Duplicate inherited names stay distinct, and
reordering slots is a change. Paths use zero-based indexes, for example
`#1.properties[0].value` and `suspendedTasks[0].vm.stack[0].pc`.
`ignore_fields` accepts top-level model names such as `suspendedTasks` or
`connections`; `max_diffs` limits returned differences.

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

## Serialize selected reader values

Use the exporter's cattrs hooks for projections as well as whole databases.
Plain `json.dumps` cannot encode `ObjNum`; `default=str` changes numeric IDs
to strings and cannot convert typed map keys.

```python
import json
from lambdamoo_db.exporter import to_json, to_json_data, to_json_file

projection = {"parents": db.objects[0].parents}
text = to_json(projection)
# Or combine the unstructured projection with your own JSON document:
text = json.dumps({"result": to_json_data(projection)})
```

`to_json_file(value, stream)` supports the same projections. Typed scalars
become integers recursively without changing the input. This preserves the
existing export format, including its limitations: distinct typed map keys
can collapse to the same integer, and JSON object keys become strings.
Use the MOO reader/writer for typed round trips. Unsupported values raise
`TypeError` instead of silently becoming null; the MOO `CLEAR` sentinel
retains its existing null representation, including in `to_json_data` results.
Reader-populated fields declared `init=False` (including properties, verbs,
and task state) are included. Sets such as recycled object IDs export as arrays.
