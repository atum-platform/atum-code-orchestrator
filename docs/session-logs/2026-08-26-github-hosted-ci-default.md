# GitHub-hosted CI default

- Returned the test workflow to `ubuntu-latest`.
- Added `actions/setup-python` for Python 3.11 so the workflow no longer depends
  on a persistent machine toolchain.
- Preserved fork admission, concurrency cancellation, timeout, and test scope.

Verification: `actionlint` and the repository unit test suite.

The hosted canary exposed one test that required the recorded interpreter path
to remain a regular file. The contract under test is stable launch identity, so
the assertion now requires an absolute Python executable identity without
assuming persistence of GitHub's ephemeral tool-cache path.
