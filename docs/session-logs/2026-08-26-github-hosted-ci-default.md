# GitHub-hosted CI default

- Returned the test workflow to `ubuntu-latest`.
- Added `actions/setup-python` for Python 3.11 so the workflow no longer depends
  on a persistent machine toolchain.
- Preserved fork admission, concurrency cancellation, timeout, and test scope.

Verification: `actionlint` and the repository unit test suite.
