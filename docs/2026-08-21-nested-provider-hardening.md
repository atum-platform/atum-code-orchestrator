# Nested provider hardening

## Behavior

Claude jobs launched by the ACO supervisor explicitly disallow Claude's
built-in `Agent` tool. A provider session cannot recursively spawn child Claude
workers that bypass parent-job concurrency, cancellation, and quota accounting.

Provider turn ceilings remain disabled as a separate lifecycle policy: queue and
run deadlines plus cancellation are the bounds.

## Verification

- Restarted `com.atum.agent-job-supervisor` through launchd.
- Confirmed the supervisor Unix socket responds to `route_status`.
- Confirmed the Claude command builder includes `--disallowed-tools Agent`.

## Follow-up

Keep the parent-owned orchestration rule in the provider command builder and
add a regression test that asserts nested Agent tools are disallowed for both
read-only and implementation Claude jobs.
