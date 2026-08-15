# Kimi billing-cycle quota detection

## Problem

Kimi Code 0.34 reports account exhaustion as `You've reached your usage limit
for this billing cycle`. The quota broker only recognized other word orders, so
the failed job remained `provider_exit`, Kimi health returned to `unknown`, and
later default code-review routes tried Kimi again.

## Change

- Recognize Kimi's current billing-cycle error wording as a canonical quota
  failure.
- Use a 24-hour cooldown when Kimi supplies no reset timestamp. This limits the
  system to one recovery probe per day while still allowing automatic recovery.
- Cover both broker parsing and supervisor persistence with regression tests.

## Verification

`python -m unittest tools.tests.test_agent_quota_broker
tools.tests.test_agent_job_supervisor` passes. Existing test teardown emits
Python 3.14 SQLite `ResourceWarning` messages but no test failures.

## Follow-up

CodexBar currently writes quota history for Claude and Codex only on this
machine. Once Kimi history is available, ACO will use its reset timestamps and
pressure instead of daily failure probes.
