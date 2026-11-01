# Parser performance

## Goal

Make database inspection substantially faster using the actual
`C:/Users/Q/code/barn/mongoose.db.new` workload. Preserve parsing, validation,
typed values, exact line boundaries, and existing CLI output.

## State

Q explicitly authorized execution of the presented plan: "Fully execute,
please," followed by "Make it so!" Implementation is on perf/parser-performance
in the isolated lambdamoo-db-py-performance worktree. PR #33 is open against
MongooseMoo/lambdamoo-db-py main. Its CI passed: 1,086 passed, 1 skipped.
Implementation, repeated measurements, final profiling, and real-dump
roundtrip verification are complete. Final documentation is being included
in the substantive commit; its resulting PR checks must finish before handoff.

Input: 221,639,217 bytes, last modified 2026-10-26 06:26:01 UTC. Python 3.13.5.
`moodb --db C:/Users/Q/code/barn/mongoose.db.new --no-cache info` reports:
49,696 numbered objects, 1,792 anonymous objects, 234,045 recycled objects,
24,998 verb definitions, 24,578 nonempty programs, 506 players, 1,178 dollar
names, 41,759 waifs, two queued and two suspended tasks.

Ran the CLI through cProfile in memory (no source contents printed or profile
files written). Instrumented time: 200.279 seconds; 369,959,594 calls.
Key profile entries (seconds include instrumentation):

| Function | Calls | Self seconds | Cumulative seconds |
| --- | ---: | ---: | ---: |
| reader._LineSource._next | 44,754,045 | 30.335 | 45.049 |
| reader._LineSource.readint | 38,903,448 | 22.395 | 61.822 |
| reader.Reader.readInt | 38,903,448 | 15.975 | 77.797 |
| Property constructor | 8,785,967 | 17.304 | 23.507 |
| reader.Reader.readProperties | 51,488 | 15.248 | 170.752 |
| reader.Reader.readValue | 16,045,919 | 13.457 | 82.383 |
| typed scalar constructor | 10,373,050 | 11.099 | 11.099 |
| reader.Reader.process_propnames | 51,488 | 4.256 | 17.348 |

An unprofiled run overlapping the profiler took 77.368 seconds; this is not an
isolated benchmark. The subsequent isolated unprofiled baseline completed in
82.605 seconds with identical summary output. These are single observations,
not a statistical performance claim; the next implementation comparison must
use repeated isolated runs.

## Decisions and why

Smallest proposed change: approximately 100-180 production lines across the
existing reader, model, index, and CLI; reuse existing tests and add only
behavior coverage needed for changed paths. No new dependencies, file format,
cache system, daemon, or parallel parser.

1. Replace the Python byte-search/tuple/slice line-reading hot path with an
   in-memory binary stream using native readline, preserving `data`, `offset`,
   `last_line_start`, LF/CRLF behavior, EOF detection, and integer diagnostics.
   Keep a single shared line source for normal, lazy, and split readers.
   Measure the actual dump after this change; retain it only if faster.
2. Remove pointless attrs assignment hooks from `Property.propertyName` only:
   it has no converter or validator, but inherited name assignments currently
   enter attrs hooks millions of times. Preserve permission conversion and
   all other model behavior. Measure separately.
3. Record verb-definition and nonempty-program counts in the existing index
   during its existing full parse. Implement `info` using those counts and
   indexed object/anonymous/recycled counts on the lazy path, avoiding its
   unconditional `list(db.objects.values())` full load. Preserve empty versus
   absent program semantics. The existing parser-source cache key invalidates
   the old index automatically. Header tasks and dollar names still use the
   current supported readers.
4. Reprofile after these changes and continue within the same parser/inspection
   scope if substantial cost remains. No promised speedup before measurements.

Evidence read: reader.py line source, value/map/waif readers, property loading
and inherited names; database.py Property and ancestors; lazy.py complete
index construction and lazy access paths; cli.py _db and info; split.py offset
consumers; tests/test_full_diff_loading.py delimiter/error contracts;
tests/test_lazy.py existing eager/lazy equivalence; split map-fidelity tests;
.github/workflows/test.yml.

## Surprises

Even cached `info` requests a full load at cli.py:234. A fresh index also builds
the entire semantic model at lazy.py:104. The 221.6 MB source dump expands into
8.8 million properties; parsing and allocation, rather than just disk I/O,
are the measured bottlenecks. The workload file was not modified.

## Implementation measurements

The shared line reader now uses BytesIO; byte offsets are computed from its
cursor when requested, rather than maintained in Python on every line.
Property names bypass unused attrs assignment hooks; permission conversion
remains intact. Existing index records now include verb definition and
nonempty-program counts, and cached info avoids full materialization.

First line-reader-only observation: 65.551 seconds, identical summary.
Reader plus property hooks, before deferred offset arithmetic: 68.360 seconds,
2,520,498,176-byte peak working set. Variation means these individual runs do
not isolate the hook change's effect. Final repeated measurements are pending.
The two actively edited test files passed 130 tests before the final offset
refinement; PR CI subsequently passed with that refinement and the full suite.

Final-code measurements so far (seconds; peak working set bytes):

| Workload | Seconds | Peak working set |
| --- | ---: | ---: |
| Baseline full parse, fresh process | 83.286 | 2,520,223,744 |
| Updated full parse, run 1 | 66.724 | 2,520,829,952 |
| Updated full parse, run 2 | 71.408 | 2,520,236,032 |
| Updated cold index + info | 67.924 | 2,548,232,192 |
| Updated cached info, first measurement | 0.112 | 86,618,112 |
| Baseline cold index + info | 86.407 | 2,546,237,440 |
| Baseline cached info | 84.359 | 2,571,980,800 |
| Updated cached info, repeat 1 | 0.098 | 86,667,264 |
| Updated cached info, repeat 2 | 0.098 | 86,548,480 |

Every measured info output has the same SHA256 (computed privately; no source
or private values printed). Full parsing improves time but does not reduce
peak model memory; the large time/memory reduction applies to cached info.
Repeated measurements and real-dump roundtrip are complete. Cached info
is approximately 860 times faster; cold index construction is 21 percent
faster; full parsing is approximately 14-20 percent faster across these runs.

The real-dump `roundtrip` command reported `identical: 221639217 bytes`, taking
139.418 seconds for the combined read/write/compare, with a 3,175,256,064-byte
peak working set. The dump's size and modification time remained unchanged
through measurement setup. No dump or generated cache is in the PR.

Final full-load cProfile: 175.318 instrumented seconds and 310,797,307 calls,
versus 200.279 seconds and 369,959,594 calls before. Reader.readInt cumulative
time fell from 77.797 to 51.008 seconds; the shared line-reading helper fell
from 45.049 to 18.580 seconds; inherited property-name processing fell from
17.348 to 11.514 seconds. Profiling overhead makes these unsuitable as wall
benchmarks; the uninstrumented table above is the performance comparison.
The remaining full-load cost includes constructing all 8.8 million Property
records and 10.4 million typed scalar records; their representation is intact.

Private measurement helper: C:/Users/Q/AppData/Local/Temp/moodb-performance-measure.py.
It invokes the actual CLI and records duration, Windows peak working set, and
summary-output hash. Run with the existing main checkout's .venv Python from
the selected worktree so imports select that worktree's code.

## Validation and delivery

After authorization, use an isolated branch/worktree from the confirmed
current integration branch; this checkout contains unrelated untracked work.
Measure isolated before/after full loading, cold indexing, and warm inspection
on the same dump, reporting time and process memory separately. Check exact
`info` output and existing byte-preserving roundtrip behavior on the real dump
without exposing content. Extend existing behavioral tests where needed;
the full suite runs on the PR through the existing CI, not as a local push
gate. Push/open the PR and wait for terminal checks; fix in-scope failures.
Do not merge without the required authorization.

## Next step

Wait for the documentation-amended PR checks, then report the measured result.
PR #33 remains open and unmerged; merging requires Q's authorization under the
approved plan. No implementation or measurement work remains.
