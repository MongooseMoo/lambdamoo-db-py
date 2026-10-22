# Select diff evidence before encoding

## Goal
Avoid encoding fields and reachable heap graphs excluded from a structured diff's selected sections. Preserve typed evidence for selected fields. No changes to census/PR28 or the plain diff path.

## State
Isolated branch `fix/diff-selected-evidence` from `mongoosemoo/main` fbe7c61. Main checkout and other worktrees untouched. Ready PR https://github.com/MongooseMoo/lambdamoo-db-py/pull/30. Initial head b5445a3 passed hosted CI; root then identified an evidence-parity error. No local CI run.

## Decisions and why
Move field selection before attribute reads and recursion only when heap evidence is excluded. Heap-selected reports retain the original encode-then-restrict path: their graph historically includes nodes reached through omitted root fields. Keep the encoder's default complete behavior for all other callers. No cache or new framework.

## Surprises
The current `evidence` encodes an entire object and heap before discarding unselected fields. Lifecycle events pass whole objects, so code-only reports traverse property graphs they never display. Applying preselection when heap was selected also removed historically retained nodes; root found this in the real objects+heap lifecycle path. Optimization is now confined to heap-excluded reports.

## Next step
Push the evidence-parity correction and read replacement hosted CI. Root independently reviews the delta. No new tests/guards requested. Large-world performance has not been measured; this removes excluded encoding work, not initial dump parsing or identity discovery.
