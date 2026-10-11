# Database inspection

Read `README.md` for the supported `moodb` commands before inspecting a dump.
Prefer the CLI to Python one-liners importing package internals. Check
`moodb COMMAND --help` for arguments; use `batch` to reuse loaded records.

For traversal size, use `contents REF --summary` or `children REF --summary`.
Add `--format json` for machine-readable counts. Inspect missing and repeated
reference counts before trusting totals; shared inheritance is not a cycle.

When recurring analysis needs a missing operation, add it to the inspection API
and CLI, with behavioral tests and README examples. Keep database dumps and
inspection artifacts out of commits, and never print credentials from a dump.
