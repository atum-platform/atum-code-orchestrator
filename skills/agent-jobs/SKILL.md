---
name: agent-jobs
description: Route focused native Codex subagents and durable cross-agent reviews, consultations, planning, design, copywriting, research, or explicitly delegated implementation among Codex, Claude, and Kimi. Use when a separable worker or independent model can materially improve a checkpoint, or when the user explicitly asks one provider to perform scoped work. The skill owns routing policy and lifecycle feedback.
---

# Agent Jobs

Keep the calling agent responsible for scope, repository state, verification, and
the final decision. Use another provider as an independent specialist, not as an
unverified authority.

## Choose the workflow

- **Independent review or consultation:** use the guarded read-only job interface.
  Read [review-rubrics.md](references/review-rubrics.md) for the relevant rubric.
- **Explicit delegated implementation:** use the bundled delegation script only
  when the user names or clearly requests another provider to do substantive work.
- Skip delegation for trivial, mechanical, or immediately verifiable work.
- Never delegate back to the provider that called this skill. Never ask a
  delegated provider to delegate again.

## Route independent reviews

The supervisor exposes a versioned `route_decide` protocol. Follow its lane only
when `enforced=true`; `mode=shadow` is telemetry and creates no reservation.
During the first canary, only Codex-on-Codex decisions can be enforced. The table
below remains the fail-open policy for shadow responses or supervisor outage.

Default routing by caller:

| Caller | Code review | Planning, design, product, copy, research |
|---|---|---|
| Codex or Hermes | Kimi K3, then Opus on provider failure | Opus, then Kimi K3 on provider failure |
| Claude | Codex, then Kimi K3 on provider failure | Codex, then Kimi K3 on provider failure |
| Kimi | Codex, then Opus on provider failure | Opus, then Codex on provider failure |

An explicit user model/provider request overrides these defaults. A fallback is
for provider failure, quota exhaustion, or unusable output, not disagreement.
Do not routinely call both providers.

For an enforced protocol-v2 route that genuinely cannot complete, first report
`route_feedback=escalated`. Then call `route_decide` once more with the same
caller, surface, and `session_id`, plus the retained decision ID as
`previous_decision_id`, a typed `escalation_reason`, and brief non-secret
`escalation_evidence`. Follow the returned terminal route exactly. The supervisor
atomically permits one child, excludes the parent provider after applying current
quota/cooldown evidence, and rejects a second hop. Retry the identical escalation
request to recover its retained decision; do not invent a new chain or call both
providers speculatively. `scope_growth` and `capability_mismatch` may change the
route shape, but the caller, surface, and session identity stay fixed. An explicit
target is still subject to parent-provider exclusion.

When the supervisor returns an enforced `agent_jobs` route, use its provider and
model alias exactly. It may have rebalanced the static table using fresh local
quota evidence and canonical rate-limit cooldowns. Do not duplicate pressure
math in the skill or override an explicit user target. Stale or missing quota
telemetry is fail-open to the static table and appears in `route_status` alerts.

## Route focused Codex work

For a separable implementation, exploration, or test scope, Codex calls
`route_decide` before spawning a native worker. Pass a stable ID for the current
task as `session_id`, use protocol v2, and report
`surface_capabilities.durable_agent_jobs=true`. Report `native_subagents=true`
only when native agents are actually available. If an older supervisor rejects
v2, retry once with v1 and follow its legacy result. A `native_subagent` response includes
an active, expiring reservation plus a worker profile and model alias; the call
does not itself spawn or control an agent.

Retain the decision ID. Spawn one native worker for the bounded scope, integrate
and verify its result, then call `route_feedback` once with `completed`, `failed`,
`abandoned`, `escalated`, or `not_started`. Identical feedback retries are safe.
On task resume, call `route_reconcile` with that session's decision IDs that are
still running; omitted active reservations are released. `codex_fast` means the
current Codex Spark-class worker available on that surface. Capacity exhaustion
returns `direct`, so the primary continues the work itself.

1. Inspect the exact project and define one checkpoint, risk, and expected output.
2. Load only the relevant rubric and incorporate it into `instructions`.
3. Submit asynchronously with the exact absolute `workdir`. Set
   `context_git_diff=true` for code review and select the correct base ref.
4. Save the job ID, cursor, and exact owner. Use
   `job_read(wait_seconds=30)` for a server-side wait until progress or terminal
   state; the caller does not need to generate repeated socket polls.
5. Treat `possibly_stalled` as alive but quiet. Cancel only after inspecting status,
   elapsed time, and the hard deadline.
6. On primary provider failure, submit the fallback as a new job. Record both IDs.
7. Verify every finding against repository evidence and run checks yourself.

For work that outlives the calling task, query `job_inbox` with the exact owner
when the task resumes. Deliveries are redelivered until acknowledged. Read the
job's retained result first, then acknowledge that delivery ID; never acknowledge
work that has not been inspected. MCP is request/response and cannot inject a
tool result into a suspended model turn, so the inbox is the durable notification
boundary rather than a claim of proactive in-chat wakeup.

Use a stable idempotency key for retries of the same provider/checkpoint. Never
submit secrets, credentials, private keys, `.env` contents, or unrelated private
material. Context files must be inside `workdir`; the guarded interface redacts
common secret shapes as defense in depth.

Use the wall-clock `timeout_seconds` as the hard execution backstop. Leave
`max_turns` at its default `0`, which omits the provider turn ceiling. Set a
positive turn ceiling only when the user explicitly requests one or the task has
a known bounded interaction protocol; an arbitrary turn cap can discard an
otherwise healthy run after its tokens have already been spent.

## Use the available binding

- **Codex/Hermes with MCP:** call `route_decide`, `route_feedback`,
  `route_reconcile`, `route_status`, `job_submit`, `job_read`, `job_list`,
  `job_cancel`, and `job_inbox` from the `agent-jobs` server.
- **Claude or a shell-only session:** run `scripts/review.py` with the equivalent
  `submit`, `read`, `list`, `cancel`, or `inbox` arguments.

Both review bindings use the same safety core. Explicit implementation goes
directly to the supervisor's capability-gated write path. Read
[operations.md](references/operations.md) for exact CLI examples and recovery.

## Delegate implementation

For explicit substantive delegation, run `scripts/delegate.py` with provider,
model, mode, absolute workdir, and one bounded prompt. `implement` permits scoped
reads and edits but no Bash, Git, external messaging, or nested agents. The calling
agent runs tests and Git operations afterward.

Kimi submissions may omit `model`; the supervisor then selects
`kimi-code/k3`. It canonicalizes supported K3 and K2.7 aliases and maps stale or
unknown Kimi aliases to `kimi-code/kimi-for-coding` (K2.7), recording both the
requested and effective model. Explicit Claude and Codex jobs still require a
model.

Use these canonical model aliases:

- Claude `opus`: architecture, UI/UX, visual design, product judgment, copywriting.
- Claude `sonnet`: ordinary implementation when Claude is explicitly requested.
- Claude `fable`: only when explicitly requested or its capability fits the task.
- Kimi `kimi-code/k3`: default for code-heavy review or implementation.
- Kimi `kimi-code/k3-256k`: K3 with lower context and quota use.
- Kimi `kimi-code/kimi-for-coding`: K2.7 for routine coding work or fallback.
- Kimi `kimi-code/kimi-for-coding-highspeed`: faster K2.7 when the plan supports it.
- Codex: pass the currently configured Codex model when another caller requests it.

After completion, inspect the complete diff, reject unrelated changes, run focused
and end-to-end verification, update durable documentation/session logs, and own
the final result.
