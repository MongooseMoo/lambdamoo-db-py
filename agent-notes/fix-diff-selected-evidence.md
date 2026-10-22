# Select diff evidence before encoding

## Goal
Avoid encoding fields and reachable heap graphs excluded from a structured diff's selected sections. Preserve typed evidence for selected fields. No changes to census/PR28 or the plain diff path.

## State
Isolated branch `fix/diff-selected-evidence` from `mongoosemoo/main` fbe7c61. Main checkout and other worktrees untouched. Source delta is 24 insertions and 32 deletions across `compare.py` and `diff_types.py`. No local CI run.

## Decisions and why
Smallest change: move the existing field-selection predicate into `encode_value`, before attribute reads and recursion, and omit heap scheduling when heap evidence is excluded (roughly 30–50 changed source lines). Keep the encoder's default complete behavior for all other callers. No cache or new framework.

## Surprises
The current `evidence` encodes an entire object and heap before discarding unselected fields. Lifecycle events pass whole objects, so code-only reports traverse property graphs they never display.

## Next step
Publish a ready PR and read hosted CI. Root is independently reading the delta; no reviewer slot was available. No new tests/guards requested. Large-world performance has not been measured; this removes excluded encoding work, not initial dump parsing or identity discovery.
