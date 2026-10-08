# Full database diff and history reports

Status: implementation contract for schema version 1.
Authoring provider: OpenAI (Codex). Independent review must use other providers.
Baseline: lambdamoo-db-py main at `5d29ccb6df0254831588e75a964071ad16d884f6`.
Audience: external coding agents and people reviewing Mongoose checkpoint history.

## Purpose and chosen defaults

Answer "what changed, where, and between which checkpoints?" with an evidence report that an agent can read in small pieces. Use one typed comparison engine for terminal output, structured events, and an indexed directory of reports. Do not ask agents to infer property boundaries from raw dump lines or consume the whole database.

The default is a **world** view: all permanent object definitions, stored property values, verb metadata/source, player membership, recycled-object membership, and reachable anonymous objects/waifs. Group changes into code, definitions, values, and heap. There is no heuristic that declares a property unimportant or volatile. Frequently changing gameplay values remain visible.

A **checkpoint** view includes every reader-supported stored field, saved execution state and serialization metadata. A **code** view selects verb definitions, order, metadata and exact source, including removals. Report the selected view and every exclusion. World equality means equality within that view, never "the dumps are identical."

The primary agent artifact is a directory with a short Markdown overview, an object index, per-object Markdown details and typed JSON Lines. Plain text remains convenient for a single pair. JSON Lines is the machine contract; Markdown provides discoverability and reading context. A browser or live server is unnecessary.

## Evidence motivating the design

Read-only corpus inventory established that historical whole dumps and modern lossless split records coexist, and that a busy adjacent checkpoint pair can change thousands of object files while changing only a handful of verb files. File counts are not semantic change counts. This motivates grouping without declaring property changes noise. The server checkout is shallow, while remote graph metadata confirms older history; old snapshot parsing and uninterrupted coverage remain unverified. Private dates, counts and revision identifiers remain in local working notes rather than the eventual public spec.

Current split checkpoints use MANIFEST, header.moo, objects/, anon.moo, verbs.moo and verbs/ under a configurable prefix. A historical flat/ export is lossy and cannot supply authoritative values. Local snapshots are candidate fixtures pending parsing and digest verification.

ToastStunt source at `aecc51e9449c6e7c95272f0f044b5ba38948459e`, `src/waif.cc:865` and `src/db_objects.cc:405`, assigns waif save indices on encounter and allocates anonymous dump numbers when needed. Neither numbering scheme is a promised persistent cross-checkpoint identity. Reordering saved tasks alone can renumber heap values.

Production fixtures stay private. Public tests use synthetic fixtures or separately approved sanitized extracts. No historical value, key, or credential is copied into these documents.

## Commands

Interface, keeping the existing NEW database convention:

```sh
moodb --db new.db diff old.db
moodb --db new.db diff old.db --view checkpoint --format jsonl
moodb --db new.db diff old.db --view code -o '#852'
moodb --db new.db diff old.db --output-dir review-pair
moodb --db new.db diff old.db --output-dir review-pair --redact rules.json
moodb history --repo /path/to/mongoose_db --from OLD_SHA --to NEW_SHA --output-dir review-history
moodb history --repo /path/to/mongoose_db --from OLD_SHA --to NEW_SHA --object '#852' --mode adjacent
moodb diff-read review-pair --event EVENT_ID --side old --range 1:80
moodb diff-read review-pair --object '#852' --category code --offset 0 --limit 50
```

`diff`: OLD is the positional argument; NEW comes from `--db`. Validate view, format, selectors and output options before loading. Views are `world` (default), `checkpoint`, and `code`; formats are `text` (default) and `jsonl`. `--output-dir` writes a bundle and prints a brief location/summary. Format and output-dir cannot be combined.

`-o/--object` is repeatable and accepts existing inspection reference syntax. A numeric ID can select an object removed from NEW; accept it if either side has that slot, otherwise fail. Resolve named references separately in each side; if they identify different permanent IDs or resolve on only one side, reject with available IDs and require numeric selectors. Include reachable heap dependencies; do not implicitly select descendants or ancestors' changes. Ancestors needed to label stored slots are loading dependencies only.

`--section` selects declared sections from the view; `--kind` filters displayed event kinds after comparison. Both are repeatable and use validated enum names. Record all filters. Status and exit code depend on selected sections before kind filtering; report both selected_changes and displayed_events. An omitted kind cannot turn a changed scope into an equal one.

`--context N` controls source hunk context (default 3, nonnegative). `--max-events N` limits displayed events (default text 200; unlimited for JSON Lines and bundles), while scanning remains complete. Zero means summary only. Negative values fail. --stop-after N, positive only, stops after N comparison events before kind filtering, presentation limits or redaction coalescing; both definite changes and identity diagnostics count. Reaching the threshold conservatively marks scan_complete false and status error, even if it might coincide with natural exhaustion; no look-ahead claim of equality is made. Never confuse display truncation with stopping comparison. Options apply identically to every edge of a history report.

Reject --stop-after combined with --redact before loading in diff and history. A pre-redaction stop threshold can reveal concealed child-event counts; redacted reports always complete the selected comparison scan and apply presentation limits afterward.

`diff-read` reads a completed bundle, not a live dump. It emits a selected event, an object's event page (zero-based offset, default limit 50), or a bounded old/new payload range. --range START:END uses half-open bounds: one-based lines for source, zero-based Unicode code points for strings and zero-based entries for lists/maps, explicitly stated in output. Category filtering happens before page selection. Object selection uses numeric permanent IDs or the bundle's typed heap IDs, never changing source aliases. --edge EDGE_ID selects a pair inside a history bundle; omitting it permits history-index queries but rejects ambiguous event/payload retrieval. It honors bundle redactions and never opens original sources to recover concealed values.

`history` and `diff-read` are self-contained and do not use `--db`. Ignore MOODB for these commands, including its path validation; reject only a command-line --db. Their CLI dispatch must distinguish the option's parameter source before validating the dump path. Core pair comparison never invokes Git, SSH, checkpoints, MOO code or an agent provider. History uses a local Git repository; users can materialize checkpoints on a server and transfer completed reports separately.

## Comparison semantics and identity

### Permanent objects and world lineage

Pair permanent objects by numeric ID, in a namespace distinct from anonymous objects. Names and `$aliases` are labels only, with OLD and NEW labels retained. The pairing claim is "same numeric slot", not proven continuity of the entity over years. Record `identity_basis: object-number` and an explicit lineage assumption.

An absent, recycled, or present slot has a distinct state. Absent -> present is added; present -> absent is removed; every transition involving recycled is one changed lifecycle event with explicit OLD/NEW states and corresponding object payloads. Suppress duplicate recycled-membership events for the same lifecycle transition. If two sampled snapshots both contain the same ID, intermediate recycling cannot be inferred. Adjacent history can reveal observed lifecycle changes, but missing history cannot be reconstructed. Do not guess identity from names, content hashes or ownership. An explicit cross-world renumber mapping is future work; report cross-world comparisons as unsupported unless the caller accepts numeric-slot pairing.

The current model puts both kinds in one object dictionary. Namespace separation applies to report identity, not duplicate IDs allowed in one dump: reject a duplicate record ID. A permanent ID becoming anonymous is a permanent removal plus reachable heap addition, never a paired permanent-object edit. Anon(-1) is a typed null anonymous reference, compared without graph traversal. Dangling non-null heap references emit an error diagnostic; existing dangling permanent object references remain typed values with missing-target annotations.

### Verb definitions and source

In world/code views pair by permanent object ID, exact complete names, direct-object argument bits, raw preposition code, indirect-object argument bits and duplicate occurrence. Share the signature owner with inspection._verb_keys; preserve its existing rendered-signature equivalence for valid inputs and validate argument bits before rendering. Invalid/reserved argument encodings fail with a structured error instead of IndexError or invented labels. Owner and nonargument permissions are compared metadata, not identity. Report changes to the verb order separately, retaining OLD and NEW slot indexes. Rename or argument changes are removal plus addition; never infer a rename.

Checkpoint view pairs raw verb slots. Verb.object is derived from the containing object; validate that invariant for in-memory inputs without claiming it is independently stored. Exact source comparison preserves indentation, blank lines and trailing spaces. It distinguishes absent program (`None`) from empty program. Use difflib.SequenceMatcher(None, OLD_lines, NEW_lines, autojunk=False); moves may be deletion plus insertion. Emit unified hunks with OLD/NEW one-based line ranges and exact lines. A source change is one event per verb, containing all hunks; it does not generate a parallel event per line. Added/removed verbs include metadata and their entire source as payload.

Every source-change event also retains full OLD and NEW programs as retrievable payloads, so an agent can request surrounding code beyond the default hunk context without reopening a historical dump. --context affects the Markdown/text hunk preview, not the underlying source evidence or event ID.

The existing `added` command keeps its stripped alignment and added-lines-only contract.

### Property slots and inheritance

Checkpoint view compares raw zero-based slots, including name, typed value, owner and permissions; duplicate names and order remain significant.

World view labels stored slots using the defining object's ID and own definition position/duplicate occurrence under ToastStunt ancestry order. Pair definitions by definer ID, exact name and duplicate occurrence. Preserve OLD and NEW raw slots and definition positions in every event. A changed inherited layout produces a definition/layout event; aligned existing values compare against their matching definition rather than causing a positional cascade.

Only stored values are compared by default, including CLEAR; do not replace them with effective inherited values. Definition additions/removals emit one definer event with affected-descendant counts; per-descendant value events are needed only for non-clear stored values. A parent's value change is not repeated for clear descendants. Property rename, including case-only spelling changes, is removal/addition because the report compares exact stored definition spelling; server lookup remains case-insensitive. Parent order, definition order and slot order changes remain explicit even when paired values are equal. A slot count inconsistent with the ancestry definition count, nonstring synthesized names, or nonunique provenance triggers a raw-slot fallback with identity uncertainty for that object. Do not merge duplicate names.

Waif sparse properties preserve class/owner, slot numbers, omitted versus explicitly stored values and propdefs length. World names are annotations resolved from each side's class definitions; raw slots remain authoritative until a unique definition mapping is established. Invalid or missing class/ancestor references remain visible as diagnostics and unresolved identity, rather than fabricated labels.

### Heap correspondence, aliasing and cycles

World heap comparison is anchored correspondence, not guessed persistent identity or unrestricted graph isomorphism:

1. Begin at paired stored slots on permanent objects. Matched map keys and equal-length list positions provide deterministic paths; source programs are not reference roots. Membership in the players/recycled sets does not create heap roots.
2. A paired anonymous/waif reference proposes an OLD -> NEW node correspondence of the same kind. Traverse fields using the same typed rules. Keep a bijection and a visited pair set so cycles terminate and sharing stays observable.
3. Build correspondence from all selected roots before finalizing events. Repeated references must agree. A many-to-one, one-to-many, or conflicting proposal produces an `identity_conflict` event including the competing reference paths. Do not let root visitation order silently choose one.
4. Dump-local renumbering alone produces no world value change when all paired anchors and the bijection agree. Emit correspondence evidence in the bundle manifest. This means observational correspondence at the anchors, not proof that a replaced equal-valued waif is the same historical instance.
5. Unequal-length lists containing heap references, and maps whose reference keys require an unknown correspondence, are unresolved regions. Emit before/after collection payloads and `identity_unresolved`; do not infer shifted identities by value similarity. Matched nonreference map keys and known correspondences can still yield precise nested events.
6. Added/removed roots emit reachable subgraph payloads with finite node tables, retaining aliasing and cycles. Orphan heap records not reachable from the selected world roots are excluded with declared counts. Checkpoint view compares all raw heap indices and records instead.

Build correspondence within the selected roots only and state that scope; a scoped correspondence need not establish global aliasing equivalence. A conflicting component is not traversed using an arbitrary winning mapping: emit its paths and finite OLD/NEW subgraph payloads, while continuing independent components. Bijection conflicts prove changed aliasing; unresolved correspondence alone does not prove a change. Cycles terminate and are not conflicts unless sharing differs. Reports keep definite changes separate from uncertainty, according to status precedence below. Full checkpoint comparison remains available for inspecting raw evidence. No user-defined code runs to resolve references.

### Typed values, ordering and precision

Never compare the lossy JSON export. Tag integer, Boolean, object reference, anonymous reference, waif reference, error, catch, finally, float, string, list, map, clear, none, unset, and missing distinctly. `missing` means no record/entry; `unset` means an absent reader-populated attribute. `CLEAR` is neither none nor missing.

List decision table: equal-length lists compare positionally with nested changes and independently paired OLD/NEW index paths; unequal-length lists consisting entirely of scalars other than anonymous/waif references use difflib.SequenceMatcher(None, OLD_tokens, NEW_tokens, autojunk=False) over canonical typed tokens (permanent object numbers are eligible); all other unequal-length lists produce a single changed collection event with complete OLD/NEW payloads. A scalar edit event carries edit opcodes (insert/delete/replace), each with OLD and NEW zero-based half-open index spans and exact values. This is one changed container event, with no duplicate child events. Structured collection replacement with heap references also carries identity_unresolved diagnostics; the different length remains a definite change. Same-length reorders compare as positional changes; do not silently treat a list as a set. Heap rules above take precedence.

MOO map entry order is ignored in world view and compared explicitly in checkpoint view; the reader must retain serialized insertion order. Keys compare by verified server key equality and type rules, or established reference correspondence, not human string rendering. Canonical sorting uses type tag followed by canonical value bytes. Mixed tagged keys such as 1, #1, and E_TYPE remain distinct.

The current Python dict model can collapse numeric keys such as integer 1, Boolean true and float 1.0. Before any exact/checkpoint release, establish the server's legal key/equality semantics with fixtures and either preserve every legal distinct key in the parsed model or fail explicitly when loss would occur. At minimum compare the declared serialized entry count with retained entries and fail on any silent overwrite/collapse; never infer absence of lost keys after parsing. Cover signed-zero and NaN keys as well as bool/int/float in the server-semantic evidence. Float VALUE comparison below is separate from map-key equality, which follows the verified server contract.

V1 declares `toast-num64-int32-gnu-v1-error-union64` as its numeric map-key policy. Integer, object-number and error keys have signed 64-bit serialized wire values; key equivalence uses their low 32 bits, separately tagged by type. Toast writes error keys through its 64-bit union member but compares the 32-bit enum member; retain the full wire value rather than discarding the upper bits. Exact Toast comparison code compiled against its headers/configuration passed 392 integer/object and 324 error boundary pairs at both -O0 and release-style -O3. Num/Objid are 64-bit, the error enum is 32-bit and the C int comparison return narrows subtraction to 32 bits. This is the tested GNU server configuration, not a universal C++ or arbitrary-server claim. Ordinary scalar values keep their full parsed precision. Checkpoint view still reports exact raw key changes even when world key equivalence holds. NaN, NUL-containing string keys and out-of-width numeric keys are unsupported. Duplicate serialized server-equivalent keys, including repeated waif references produced by a broken server tree ordering, fail before comparison; dictionary reconstruction cannot preserve their evidence. Parser acceptance is separate from server restore validity. A different compiled numeric policy requires a versioned extension rather than an implicit guess.

The verified error-union policy assumes the tested little-endian union layout. Raw split acquisition uses a private boundary scanner without semantic map reconstruction; it preserves even unsupported map bytes, while semantic loaders retain the fidelity checks above.

Default float equality compares binary64 representation, including signed zero; infinities and NaNs have tagged encodings. NaNs compare by available parsed bits. The report describes parsed-value equality, never recovery of an original NaN payload or exact numeric spelling from the dump. No default tolerance or whitespace normalization. The existing Python comparison API's float tolerance remains an explicit legacy option until a versioned API change.

### Coverage matrix

A checked inventory of reader-populated fields is part of implementation. Adding a model field without declaring its comparison coverage must fail verification. Present-versus-unset is compared for every stored attribute.

Section enum: objects, properties, verbs, heap, players, recycled, runtime, serialization. World permits the first six; code permits verbs; checkpoint permits all eight. Unsupported section/view combinations fail before loading. Categories and section mapping: verbs -> code; properties definitions/layout -> definitions, values/owner/perms -> values; object name/flags/owner and parents/children -> definitions, location/contents/last_move -> values; lifecycle -> definitions; players/recycled -> definitions; heap -> heap; runtime -> runtime; serialization -> serialization. Include section as well as category on each event. Heap-contained verb/property changes retain section heap and category heap with an explicit subtype.

Lifecycle events have primary section objects and sections_applicable containing objects plus recycled when either state is recycled. Select an event when any applicable section is selected, and emit it only once; recycled-only selection must not suppress that transition. Ordinary recycled membership events are suppressed only when their shared lifecycle event is selected. Code view's containing-object additions/removals have primary/applicable section verbs because they are verb container evidence. Selection loads ancestor/heap-root dependencies but emits no excluded-section events.

| Record / section | World | Checkpoint |
|---|---|---|
| Permanent object existence/id, name, flags, owner, location | Yes | Yes |
| Ordered parents, children, contents; last_move | Yes | Yes |
| Property definitions/count, stored slots, value/owner/perms | Definition-paired | Raw slots |
| Verbs: names, owner, packed perms, preps, order, source; derived object invariant | Definition-paired | Raw slots/all stored fields |
| Anonymous objects, waifs, shared references | Reachable anchored graph | Every raw record/index |
| Players and recycled-object membership | Yes; player order reported separately | Yes, including ordering |
| Pending finalization values | Excluded | All values |
| Clocks, queued/suspended/interrupted tasks | Excluded | All fields, queue order |
| VM locals, stack, top/vector/funcId/maxStackframes | Excluded | All fields |
| Activation headers, temp values, rtEnv, stack/code, pc, builtin data, language fields | Excluded | All fields, including unset |
| Connections, listener suffix, section-presence flag | Excluded | All records/flags |
| Format/version string, declared header counts | Provenance/diagnostics | Compared fields |
| line_ending, v4_dummy, object v4 blank/linkage fields | Provenance | Compared fields |

An object filter applies to player membership and order evidence before comparison. Player order compares the relative order of surviving selected IDs, so adding/removing a player alone does not imply a reorder, but a simultaneous survivor reorder is retained. Order payloads contain only selected numeric object IDs. Duplicate numeric player IDs are malformed input and produce an error.

Code view includes permanent object creation/removal only where a verb definition is involved, verb metadata/order and source. Object names and aliases are labels. Runtime and serialization sections are always enumerated as excluded. Counts that contradict parsed records are errors rather than silently repaired totals.

Checkpoint equality is structural equality of the supported parsed representation. Record each source SHA-256 independently; equal parsed checkpoints with unequal bytes are possible. A byte comparison is a separately labeled provenance result. Do not advertise checkpoint structural equality as byte equality.

## Agent bundle and machine schema

Bundle layout:

```text
manifest.json
summary.md
objects.jsonl
events.jsonl
objects/permanent-852.md
objects/heap-waif-HASH.md
objects/heap-anonymous-HASH.md
sections/runtime.md
sections/serialization.md
payloads/sha256-HEX.json
```

Only nonempty detail files are created. `summary.md` lists exact sources, view, filters, status, counts by category/kind, excluded sections, redactions, ambiguities and a linked object index. Sort categories code, definitions, values, heap, runtime, serialization; inside each, permanent IDs numerically. Keep the overview to at most 200 changed entities, showing the remaining count and how to use the full index. No automatic prose claims about intent, bugs fixed, authorship or causality.

`objects.jsonl` contains one row per changed entity: typed entity ID, OLD/NEW labels, category counts, detail filename, and byte ranges into events.jsonl for bounded retrieval. Indexes use binary UTF-8 byte offsets, verified against event IDs and source digests. Markdown details put verb/source changes first, then definitions/metadata, stored values and reachable heap links. They preview at most 50 events per entity, with exact remaining counts and a diff-read command; complete evidence stays in indexed JSON Lines and payloads. This Markdown preview limit does not reduce bundle details_complete. Each event names its defining object/property or verb, OLD/NEW slots, typed path, and event ID. Repeated heap nodes are rendered once and linked from their anchors. Labels are escaped untrusted text, never filenames or commands.

Every JSON Line is a complete UTF-8 JSON object. Stream records are `header`, zero or more `event`, then exactly one `footer`. Header includes schema version, tool version, source kind/bytes/SHA-256/format, optional Git provenance, lineage assumption, view and normalized options. Events include `event_id`, `category`, `kind`, typed OLD/NEW entity IDs/paths and values or payload references, plus identity evidence/diagnostics. Path segments are tagged object/field/property/verb/index/map-key/heap steps, with raw slots and definition keys where applicable; never parse a dotted display string.

Kinds are added, removed, changed, type_changed, order_changed, identity_conflict, identity_unresolved. Each event declares definite_change: true or false. Identity conflict is a definite aliasing change; unresolved correspondence is a diagnostic rather than a selected change unless it accompanies a separately proven value/length change. One-sided events use tagged missing values. Parent addition/removal carries a complete bounded payload reference rather than duplicating events for every child. Changed containers use nested leaf events, one scalar-opcode event, or one replacement event under the alignment policy, never duplicate forms. Counts distinguish definite change events, uncertainty events, changed entities and added/removed source lines.

Total event order: category rank above, section enum rank, entity-kind rank (permanent, anonymous, waif, global), numeric permanent ID or canonical anchor-based heap ID, canonical OLD then NEW typed path bytes, kind rank in the enum above, deterministic opcode/duplicate occurrence. Paths retain both OLD and NEW coordinates. Heap report IDs derive from the least canonical selected anchor path rather than dump numbers. Canonical traversal computes order before emission; discovery order must not select heap mappings. No model object address or Python hash iteration order enters output.

The `typed-exact-anchored-v2` policy normalizes reference map keys with a finite rooted certificate rather than a raw dump ID or a recursively defined final heap ID. When a bijective key pair first enters a complete discovery frontier, freeze the digest of its least canonical selected rooted OLD/NEW path evidence from that pass; that evidence may refer only to earlier-frontier certificates. Certificates retain a reference type tag, distinguish keys rooted at different paths and are usable only while the pair belongs to the current frontier. Raw key evidence and checkpoint view remain exact. Later aliases may change the least final heap path without changing the first-frontier certificate. Unsupported cyclic conditional frontiers remain explicitly unresolved.

Synthetic JSON Lines encoding examples:

```jsonl
{"type":"obj","value":"1"}
{"type":"int","value":"1"}
{"type":"map","entries":[{"key":{"type":"obj","value":"1"},"value":{"type":"clear"}}]}
```

Integers and object/dump IDs use decimal strings to avoid JavaScript precision loss. Float values use a 16-digit binary64 hex representation. Strings are decoded exactly according to the reader's Latin-1 contract, then JSON-escaped UTF-8. Lists/maps are tagged; maps are entry arrays. Recursive heap values use finite per-side node tables, never recursive JSON objects.

Payloads above 8 KiB encoded size use content-addressed references in bundles, with type, encoded length, digest and preview. Previews are at most 1 KiB and are explicitly incomplete. Files use a defined tagged JSON encoding and SHA-256; `diff-read` supplies bounded retrieval. JSON Lines without an output directory inlines values, with no hidden value truncation; callers can use max-events/stop-after if necessary.

Footer and manifest include:

- `scan_complete`, `identity_complete`, `details_complete`, `payloads_complete`;
- selected_changes (definite changes before kind filtering), uncertainty_events, displayed_events (matches before presentation limits), emitted_events, omitted_events (unknown when stopped);
- `status`: equal, different, unknown, or error;
- redacted event/value counts and excluded sections.

A complete unfiltered bundle has all event details and payloads unless explicit redaction is applied. Redaction does not undo the fact a value changed. A missing footer, unfinished manifest or missing/digest-invalid payload is an incomplete report; agents must not infer equality.

Stable event IDs are hashes of schema version, immutable source identities, view, normalized numeric object selectors, selected sections, pairing/precision policy version, typed OLD/NEW paths and event kind/duplicate discriminator. History additionally includes edge_id and ordered full OLD/NEW commit SHAs, even when identical source bytes recur. Kind filters, context, output format, presentation limits and previews do not change IDs. For redacted output hash only the post-redaction paths/evidence plus the redaction-policy digest; never hash secret keys or values into an ID. IDs are not comparable across redaction policies. Disambiguate repeated event coordinates deterministically. Omit wall-clock generation times and absolute local paths from default deterministic report content; relative source labels and caller-supplied timestamps are allowed.

## Runtime status, limits and safe publication

Status precedence: error > different > unknown > equal. Errors and explicitly stopped scans exit 2, even when a prefix contains changes. A complete scan with any definite selected change is different and exits 1, regardless of identity_complete; retain all uncertainties prominently. With no definite change and unresolved identity, status is unknown and exit is 3. Equal selected scope requires complete scan and complete identity and exits 0. Display truncation alone does not change status/exit. All diff/history/diff-read loading, selection and lookup failures exit 2, overriding older inspection commands' exit-1 convention. JSON Lines may contain a partial event prefix on failure, but must end with an error footer when possible; process exit and footer both matter.

If --kind hides every observed change, print the overall changed status/count and "no events matching selected kinds". Summary-only output still scans all selected data and reports complete counts. A difference only in excluded runtime sections does not change world status; manifest declares the exclusion, and checkpoint view can report it.

Ordinary reports apply no automatic secret detection or suppression. Sharing real-world dumps requires explicit redaction support. --redact rules.json applies identically to text, JSON Lines, bundles and diff-read, replacing selected OLD/NEW values, whole source, and labels with opaque markers in every artifact, including paths, previews, payloads, links and diagnostics. Compare first; retain change kind/type and redaction count. Omit hashes and encoded lengths for concealed values. Payload digests and event IDs are computed after redaction. Redacted node subgraphs are concealed transitively; secret map-key paths use opaque redacted coordinates and derived labels are concealed too. No original secret key/value participates in a published identifier.

Rule document v1 is an object with schema_version: 1 and rules: an array of selectors/actions. Allowed selectors: section alone; properties plus numeric definer ID, exact definition name and occurrence (integer or '*'), covering that definition's stored descendant slots/subtrees; verbs plus numeric object ID, exact full names and occurrence, covering whole source; heap plus numeric waif class ID and zero-based sparse slot; objects plus numeric ID and field from name/aliases. The action is conceal; only a section-runtime rule can instead use reveal_runtime to opt out of wholesale masking. No arbitrary regex, value predicate, literal secret map-key selector or partially revealed source region in v1. Redacted labels propagate through all label-derived annotations.

Whole-section properties selectors include selected permanent properties, reachable anonymous properties and reachable waif stored slots. Definition selectors also bind matching colon-prefixed waif class definitions, including inherited definitions, to their sparse stored slots. Whole-section verbs selectors include reachable anonymous verb definitions/source even though their comparison events are categorized under heap. Checkpoint heap coverage without an object filter includes all stored anonymous/waif records. These rules apply to lifecycle graph payloads and annotations as well as leaf events; no match outside selected report coverage is implied.

Whole-property/verb selectors conceal entire selected containers, including empty containers, and derived property-definition/program counts. All whole-section rules add a Boolean per side for eligible container coverage (0–2 per pair), never the cardinality of concealed owners or children. Selected global objects, heap, players, recycled, runtime and serialization containers match even when empty. Property/verb rules require a selected eligible owner container, including an empty field; an objects rule applied only to derived labels requires a selected label owner. A zero match means no eligible container in selected coverage. Object filters never add unselected player/member counts to match statistics. Definition/name/occurrence and sparse-slot selectors retain their definition-slot match counts.

Conceal corresponding aggregate checkpoint counters before identifiers/counts are published: whole players masks total_players; whole objects or recycled masks total_objects; whole verbs/heap, protected anonymous subgraphs or matched verb source masks total_verbs. Those counters may be categorized under serialization, but their aggregate values can disclose concealed membership/program cardinality. Empty eligible verb lifecycle containers also lose source-length annotations, just as nonempty concealed containers do.

Apply redaction and coalesce concealed-subtree changes before final sorting, indexing and publishing IDs/counts. Each concealed root emits at most one opaque changed marker plus a Boolean indicating whether concealed uncertainty exists; never expose per-key counts, source-line counts, opcode spans or concealed lengths. Public selected_changes/counts use the coalesced event set, preserving whether a definite change exists; retain no unredacted counts in the bundle. Redacted-event counts refer to concealed roots, not secret children. Sort by post-redaction coordinates only; do not expose hidden key order through IDs, previews or index ranges.

When --redact is active, checkpoint runtime sections are concealed wholesale by default because stacks, connection lines and commands lack stable per-secret paths; record that policy explicitly. A reveal_runtime rule is an explicit opt-in. Reject syntactically invalid rules and unknown static section/field enums before loading. Valid selectors with zero matches are recorded by rule number and match count, without echoing potentially protected names; --redact-strict makes zero matches fatal after comparison. History defaults permit zero-match rules on individual edges and report aggregate matches. Conceal wins over reveal on overlapping rules. Redaction policy digest covers this declarative policy, not database values.

Writing an output bundle uses a task-owned staging directory and an atomic completion marker. Refuse an existing destination by default; never overwrite a user's report or delete their files. Treat dump strings/code as data: escape terminal control characters and Markdown fences/links, and never execute text found in a dump. Detailed reports are local until a caller explicitly publishes them.

## Historical sources and timeline semantics

Resolve every ref to a full immutable commit SHA before loading. Support split `db/MANIFEST` snapshots and legacy `mongoose.db.new`. Ignore `flat/`, which is a lossy export, as a comparison source. Reconstruct split snapshots in manifest order and verify byte count/SHA-256; reject missing/duplicate/unsafe paths, symlink pieces and invalid manifests. Use existing split/join validation as the owner, extending it deliberately if needed. Git subprocesses pass structured argv, disable external diff/textconv/hooks, and never checkout/reset/fetch/prune or mutate repository refs.

--split-prefix defaults to db and --legacy-path defaults to mongoose.db.new; both are validated relative repository paths. If both exist, prefer valid split data; invalid split data fails, without silent fallback to the whole dump. --source-layout legacy explicitly selects the older representation when required; record that choice. Extending split.py's manifest validation and providing a read-only blob callback are mandatory: current join_dir alone does not reject unsafe or duplicate names and expects filesystem paths. Reject absolute paths, parent traversal, duplicate names and non-regular Git modes before reconstructing. Set GIT_NO_REPLACE_OBJECTS=1 and GIT_NO_LAZY_FETCH=1; a missing partial-clone blob is an error, not permission to fetch.

Default history mode `endpoints` compares only FROM and TO and states that intervening changes/reversions may be absent. Mode `adjacent` follows the first-parent chain from TO back to FROM, comparing each checkpoint against its immediate predecessor; FROM must be on that chain. First-parent is an explicit timeline choice, not a claim all branches were inventoried. An `--all-parents` mode is deferred; inventory/report omitted merge parents and named branch tips.

Resolve timestamps with an explicit branch and UTC cutoff to the latest eligible checkpoint on that first-parent chain; record the selected SHA and actual checkpoint/commit timestamp, which can differ. Never silently use wall-clock date or a repository default branch that can move mid-run. Require a declared branch for date selection; explicit SHAs need none.

Date CLI uses --from-date/--to-date RFC3339 timestamps with explicit timezone plus --branch REF, mutually exclusive with the corresponding --from/--to SHA/ref. Eligibility and tie-breaking use committer timestamp then first-parent chain position, not author time; select the closest-to-tip eligible checkpoint. Checkpoint time is extracted only from a recognized backup metadata field; otherwise mark it unknown, without parsing arbitrary commit prose as a factual timestamp. Resolve branch tip once before selection.

Timeline output includes endpoint overview, ordered edge index and one pair bundle per selected edge. A change followed by a revert is retained as two transitions. Event identity includes its edge, so it is never deduplicated away. Provide an object timeline linking each changed verb/property/value to its edges. Commit metadata is evidence of checkpoint time, not proof of the author or cause of a particular change. Large histories have a configured edge cap (default 100); reject over-cap before comparison and require an explicit larger cap.

History layout is manifest.json, summary.md, edges.jsonl, objects.jsonl, timelines/permanent-ID.md and edges/EDGE_ID/ containing ordinary pair bundles. Edge IDs hash ordered full commit IDs and source digests; edges.jsonl is chronological first-parent order. --max-edges controls the cap. Overall status uses the same precedence as a pair: any failed/stopped edge yields error/exit 2; otherwise any definite change yields different/1; otherwise any unknown edge yields unknown/3; otherwise equal/0. Endpoint mode has one edge and the same layout. --keep-going permits failed-edge records but preserves exit 2.

Shallow boundaries, missing checkpoints, unavailable objects, nonancestral endpoints and mismatched lineage fail or produce an explicit incomplete timeline; no claim of continuous history. Legacy corrupt/unsupported snapshots are recorded as failed edges, and adjacent mode cannot bridge them silently. Continue only with an explicit keep-going option, retaining exit 2 and a failed-edge index.

Unchanged Git blob IDs identify byte-identical pieces within the same repository, but do not prove their semantic meaning is unchanged: ancestor layouts, waif numbering/homes and reference targets may change elsewhere. Initial implementation may reconstruct/fully compare every selected checkpoint. Any optimization must prove dependency invalidation and parity with full materialization before release. Identical whole-source SHA-256 can skip comparison after validation for the declared view.

No automatic clone of the multi-gigabyte history or remote live checkpoint. Acquisition is separate: a caller can run report generation beside the existing server checkout or transfer chosen immutable snapshots. History reporting must be resumable by source digests and semantic option version; validate an edge's completed bundle before reusing it.

V1 exposes `--resume-from PREVIOUS_REPORT` and requires a new output directory. Reuse verifies every completed edge's sources, ordered commits, semantic options and artifact integrity. An existing report stays read-only; invalid evidence fails validation rather than being trusted or overwritten.

A caught edge failure or user interrupt without keep-going publishes an error report containing the authenticated completed prefix. `new_commit` records the covered endpoint and `requested_new_commit` records the original target; scan_complete is false. This report is eligible for `--resume-from`. Abrupt process termination before publication does not create a completed report.

## Implementation ownership and delivery order

1. Typed events, paths, completeness, immutable-source loading and legal typed-map fidelity/loss detection; exact pair comparison and checkpoint coverage. Keep compare.py as comparison owner, sharing typed value handling with explicit pairing/precision policies for raw checkpoint, world and legacy API traversal. Preserve published DiffPath callers in references.py. The legacy compare_databases adapter must retain float tolerance, None-change classification, historical path formatting, prefix-positional lists, early max_diffs behavior, raw waif indices and derived-field omissions, until a separately versioned API change. Do not force new source/list/identity policies onto existing callers or duplicate equality logic in renderers.
2. World/code pairing, property provenance, conservative heap correspondence and agent bundle rendering. Shared name/signature helpers stay in inspection.py; reader/database own parsed representation. This completes the first useful full pair-report release.
3. Read-only Git source adapter, endpoint and adjacent timelines, per-object timeline index and resumable edge bundles. Split validation stays in `split.py`; CLI orchestrates only. This completes the history release.
4. Measured dependency-aware blob skipping and bounded retention. Source adapters must release compared records and retain only dependency metadata/heap correspondence as permitted. Existing LazyDatabase caches every accessed object and cold indexing parses the whole dump, so the first release makes no constant-memory or instant-history claim.

CLI belongs in `cli.py`; rendering/serialization and bundle IO get focused modules. No Git, rendering, provider APIs or report-writing logic goes inside model equality. No writer/roundtrip dependency is required to compare loaded dumps; joining existing split bytes is distinct from model re-encoding.

Source stability is required from the first release. V1 comparison uses immutable byte buffers for complete sources, hashes those exact buffers and parses them without offset-index reopening. Git blobs supply immutable bytes. Local file acquisition uses before/after file identity/content verification around freezing and fails on detected replacement or in-place mutation; pinning a handle alone does not prevent in-place writes. No record is subsequently read from the original path. Cache metadata is not proof of source identity. Global --no-cache/--cache-dir do not affect v1 diff semantics; it creates no per-materialized-snapshot index. Stage 4's optional indexed source adapter must demonstrate parity and immutability before use.

Define LF-delimited byte reading consistently across full and future indexed diff adapters: decode Latin-1 and remove only the LF or CRLF record delimiter, retaining other control characters as data. Bare CR must not be interpreted as another record separator or stripped repeatedly. Reader changes belong in reader.py with roundtrip/regression checks; do not create a different string decoder inside comparison. Declare unsupported format versions as errors. The current parser dispatch supports version 17 and legacy versions 1 through 4; historical fixtures must establish actual field coverage for each before full checkpoint claims.

## Acceptance and corpus plan

Maintain a private corpus manifest with exact commit/blob IDs, SHA-256, size, format, checkpoint time, provenance and expected feature coverage. Metadata inventory, a real busy adjacent world comparison, a real code-change bundle and bounded CLI retrieval have been exercised. The world scan completed while explicitly reporting unresolved identity; it was resource intensive. The broader corpus matrix and 24-edge performance study below remain pending. Distinguish parser acceptance from server restore validity.

Required tests:

- Equal self comparison; OLD/NEW reversal swaps entity-level added/removed and before/after for fixed raw entity paths; list/source hunk alignment may differ by direction but preserves exact reconstructable evidence. Deterministic repeated output.
- V1 immutable-buffer comparison against full parse on supported LF/CRLF fixtures, plus bare-CR losslessness checks; failure on source replacement/in-place change. Stage 4 requires cold/warm/no-cache indexed parity and corrupt-cache handling before activation.
- Duplicate verbs/signatures, inserted/reordered definitions, name/argument change, removed program, whitespace-only source change and None versus empty program.
- Duplicate own/inherited property names, multi-parent order change, inserted ancestor definitions, CLEAR changes and layout-versus-value distinction.
- Typed map collisions, dotted/quoted keys, bool/int/float semantics, exact signed zero/NaN/infinity, unset/None/missing/CLEAR, and large integers.
- Synthetic pure anonymous/waif renumbering gives world equality plus correspondence and checkpoint changes; alias splitting/merging gives definite conflicts, while equal cyclic graphs terminate normally. Unequal heap lists are definitely different with unresolved correspondence; unanchored reference map keys can be unknown, never falsely equal.
- Every stored saved-state field, nested rtEnv/VM/activation, builtin state, queue ordering, connections, v4 metadata, LF/CRLF provenance.
- Display limits including zero, stopped scans, all-kind filters, summary counts, missing footer, invalid/missing payload and every exit status.
- Redaction cannot leak through keys, labels, previews, payloads, filenames or diagnostics; terminal and Markdown escaping tests use malicious strings.
- Safe output directory handling; Git refs with hostile syntax remain argv data; split manifest traversal, missing pieces, symlinks and digest mismatch fail.
- Timeline change/revert, endpoint versus adjacent semantics, merge-parent exclusions, shallow boundary, missing/corrupt edges and resume invalidation.

Private corpus minimum: a metadata-inventoried busy adjacent pair; a quiet hourly pair; a known code-edit pair; a wide-interval pair; a pre-split legacy revision; a split-transition pair; available older local dumps if parsable. Keep exact dates/commits/digests in the private corpus manifest. Use all refs to inventory sources before choosing a linear timeline. No assumption that backup commit messages describe semantic modifications. Select expected code/property changes by inspecting parsed records, without printing protected values.

Measure time, peak resident memory, bytes read, records decoded, heap mappings and output bytes for representative equal/quiet/busy/wide pairs and a 24-edge timeline. Publish workload digests, cold/warm conditions, environment and baseline values before setting numeric performance budgets. Correctness, explicit completeness and usable per-object retrieval gate delivery; speed claims require paired comparable runs.

## Glossary and remaining evidence

**Checkpoint**: one immutable serialized database snapshot. **World view**: selected persistent-world state with anchored reachable heap correspondence. **Stored slot**: serialized position, distinct from effective inherited value. **Definer**: object owning a property definition. **Anchor**: a paired permanent-object value path establishing observational heap correspondence. **Edge**: ordered OLD -> NEW checkpoint comparison. **Bundle**: indexed report directory with typed events and retrievable payloads.

Defaults are implemented for the requested external-agent workflow: world overview, exact code, typed structured evidence, per-object drill-down, explicit checkpoint view, and endpoint/adjacent history. Numeric map equivalence has been checked against the declared configured Toast comparator, with unsupported representations rejected explicitly. Remaining validation includes the broader historical corpus matrix, uninterrupted history beyond the available shallow boundary, and representative resource budgets. This release makes no streaming, server-restore or performance improvement claim.
