# Remove the dormant Kimi provider

Date: 2026-09-26

## Why

Since the OpenCode release (#33, #34), Kimi has not been a routing, CLI, or
client target. Kimi K3 is reached as `opencode-go/kimi-k3` on the OpenCode Go
subscription. The owner asked for the dormant Kimi launch code to be removed.

## What changed

- Supervisor: removed the Kimi model aliases, auth keys, CLI generation probe,
  command builder, legacy and modern runtime staging, `KIMI_SHARE_DIR`
  confinement, the `AGENT_JOB_KIMI_SEMANTIC` kill switch, and Kimi binary
  discovery. `_confine_implementation` now serves only Claude, so it rewrites
  only `--mcp-config`.
- Deleted the six Kimi agent definitions and prompts, plus `tools/empty_mcp.json`,
  which only Kimi used.
- Event decoder: removed the Kimi record normalizer. Private stdout now covers
  Claude and OpenCode.
- Quota broker: removed `kimi` from the tracked providers, its rate-limit
  pattern, and the billing-cycle daily cooldown.
- Installer: no longer writes `AGENT_JOB_KIMI_BIN` or `~/.kimi-code/bin` into the
  LaunchAgent, and no longer carries `AGENT_JOB_KIMI_CONCURRENCY` or
  `AGENT_JOB_KIMI_DEFAULT_MODEL` forward, so a reinstall drops them.
- CAO bridge and migration gate: dropped the `kimi_cli` mapping.
- Scheduler: a job still queued for a provider with no slot now fails at once
  with `launch_error`. Before this, the slot lookup raised `KeyError` on every
  scheduler pass, so no job behind it launched until its queue deadline. #34 had
  already removed Kimi's slot, so an upgrade with a queued Kimi job could hit
  this.

## Kept on purpose

- The approved-check sandbox still denies reads of `~/.kimi`, because Kimi logins
  stay on disk after the code is gone.
- The client installer's recognizers for older managed guidance still mention
  Kimi, so pre-OpenCode managed blocks keep migrating cleanly.
- The routing policy maps `kimi-` model ids to the `moonshot` family for
  OpenCode's Kimi K3.
- Historical session-log entries are unchanged.

## Tests

Kimi was the suite's stand-in for a provider with readable raw stdout. The
transport tests (cursor reads, server-side waits, cancellation, stall
classification, partial-unavailable) now submit CAO-bridged Claude jobs, which
are the remaining production path without a semantic adapter. The quota tests
moved to Claude, the byte-based stderr liveness test to OpenCode, and the
provider-neutral decoder tests to OpenCode. Kimi-only alias, kill-switch,
decoder, runtime-staging, and billing-cycle tests were deleted. New tests cover
the queued-job guard, OpenCode default-model recording and idempotency, and a
leftover Kimi key in a profile file reaching no provider.

## Verification

- `python -m unittest discover -s tools/tests`: 327 tests pass.
- The queued-job guard test fails without the fix: the scheduler logs
  `agent-job scheduler error: 'kimi'` on every pass and the job never finishes.
- `py_compile` on all tools, `ruff --select F` on the changed modules, and
  `git diff --check` are clean.

## Follow-ups

- The approved-check sandbox denies only a fixed list of credential paths. It
  does not cover ACO's own credential files (`opencode.env` and other
  `AGENT_JOB_PROFILE_ENV` entries), OpenCode's `auth.json`, or `~/.kimi-code`.
  A delegated Claude implementation job with approved checks could therefore
  make a check print one of them into the model's context. This predates this
  change and needs its own sandbox test, so it is tracked separately.
