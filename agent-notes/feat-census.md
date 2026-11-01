# Goal
Make PR #28 mergeable and merge it with a normal merge commit.

# State
The conflicts with MongooseMoo/main are resolved. Focused behavioral checks in
tests/test_census_cli.py and tests/test_source_queries.py passed on Windows.
The worktree's untracked uv.lock is unrelated and will remain uncommitted.
There are no posted PR reviews or review comments. Previous PR CI passed.

# Decisions and why
The smallest repair combines overlapping grep options and task time handling
in cli.py and inspection.py (roughly 30 changed lines plus conflict removal).
Keep the existing PR's census, slots, removal/rewrite, liveness and other CLI
work. Keep main's source-query filters, structured output and indefinite-task
label. Route grep -L through the existing source-query filters so options
continue to compose, and retain width clipping for text output.
Use PR CI for the full suite; do not run CI steps locally as a push gate.

# Surprises
PR #28 contains later removal/rewrite and liveness commits beyond its title.
Its actual branch scope is retained. The only merge conflicts are in cli.py
and inspection.py.

# Next step
Commit and push the integration, wait for PR CI, then merge and verify the
two-parent result. After merge, preserve the unrelated untracked uv.lock when
cleaning up the task worktree.
