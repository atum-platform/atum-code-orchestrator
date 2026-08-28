# GitHub Actions v7 refresh — 2026-08-28

PR #25 advanced `actions/checkout` to pinned v7.0.1 and merged first. PR #26
then conflicted on the adjacent workflow lines while advancing
`actions/setup-python` to pinned v7.0.0. Dependabot automatically rebuilt its
branch on current `main`, preserving both reviewed SHA updates without changing
workflow behavior, permissions, runner selection, or Python version.

Verification on the refreshed lineage: YAML parsing, Python compilation, and
the full Python 3.11 suite (`293` tests) passed locally. The fresh exact-head
hosted `test` job remains the authoritative merge gate; the earlier green result
belongs to the pre-parent head and is history only.
