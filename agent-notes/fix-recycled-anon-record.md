# Recycled slot and anonymous definition extraction

## Goal

Fix lambdamoo-db-py's rejection of Barn/Test.db during the authorized parser
fuzz-corpus work. Q explicitly requested this repair after approving the plan.

## State

Branch `fix/recycled-anon-record`; source and existing reader tests updated.
Focused changed-file pytest completed successfully. Real Barn/Test.db now
extracts all 444 declared programs, representing 64 exact-byte unique sources;
anonymous #15529 and its recycled permanent marker both survive. Not yet
pushed or published. The original checkout's dirty work is
untouched. This is a dependency repair for Barn issue #414.

## Decisions and why

Real input: Barn/Test.db contains `# 15529 recycled` at line 217620 and anonymous
`#15529` at line 217702. The prior load failed at the latter with `duplicate
object record id`. ToastStunt db_file.cc ng_read_object leaves recycled slots
empty; db_objects.cc db_read_anonymous can allocate an anonymous object into
such a slot. A recycled placeholder is not a live object definition.

Correct the existing duplicate-ID check at the transition into anonymous
records: release recycled permanent slot IDs from the seen-definition set.
Keep permanent live definitions reserved and record each anonymous definition
normally, rejecting duplicates within and across anonymous batches. Keep the
model's recycled permanent slots intact for faithful export. No separate ID
namespace or database-model change is necessary for this overlap.

The existing duplicate-definition test now covers repeated anonymous definitions
of a recycled ID. A reduced regression from the reported layout exercises eager
loading, verb extraction, writing, splitting/reassembly and indexed loading.

## Surprises

The v17 writer already emits recycled permanent slots before anonymous records;
the index reader already excludes recycled placeholders from its records map.
The shared reader check was the point preventing both supported read paths.

## Next step

Commit, push, open a PR and wait for its CI. Continue Barn fuzzing afterward.
