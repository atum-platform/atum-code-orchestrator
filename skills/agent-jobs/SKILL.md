---
name: agent-jobs
description: Route focused native Codex subagents and durable cross-agent reviews, consultations, planning, design, copywriting, research, or explicitly delegated implementation among Codex, Claude, and OpenCode. Use when a separable worker or independent model can materially improve a checkpoint, or when the user explicitly asks one provider to perform scoped work. The skill owns routing policy and lifecycle feedback.
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
In `codex_canary`, decisions are enforced for Codex callers on the Codex surface,
including Codex routes to Claude or OpenCode. `surface_canary` extends v2 enforcement
to supported Claude and OpenCode coding surfaces. The table below remains the
fail-open policy for shadow responses or supervisor outage.

Call `route_decide` before every cross-agent `job_submit`, not only before native
Codex work. Pass protocol v2, a stable task/session ID, the actual capability,
and `surface_capabilities.durable_agent_jobs=true`. Pass an explicit user target
through `explicit_provider`/`explicit_model`. When the response is enforced,
submit only the returned provider and model for an `agent_jobs` lane, or continue
locally for `direct`; the decision supersedes the static table below. Retain the
decision ID and report terminal feedback exactly once. A durable submission with
no preceding enforced route is a protocol violation unless the supervisor was
unavailable or returned a shadow decision.

Default routing by caller:

| Caller | Code review | Planning, design, product, copy, research |
|---|---|---|
| Codex or Hermes | OpenCode, then Opus on provider failure | Opus, then OpenCode on provider failure |
| Claude | Codex, then OpenCode on provider failure | In-family: native Claude worker or direct |
| OpenCode | Codex, then Opus on provider failure | Opus, then Codex on provider failure |

OpenCode runs the supervisor's configured default model, currently Muse Spark
(Meta) on the OpenCode Go subscription, so it is a different family from every
caller. It is read-only: engineering work has no automatic fallback. As a
caller, OpenCode passes `caller_provider=opencode` and never routes to itself,
because its own model family varies.

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
Fresh actual utilization at or above the supervisor's exhaustion threshold is
fail-closed for that provider, including explicit targets and one-hop fallbacks;
continue directly or use the enforced alternate instead of bypassing admission.

## Route focused same-family work

For a separable bounded scope in the caller's primary domain, call `route_decide`
before spawning a native worker. Codex native lanes cover implementation,
exploration, and tests; Claude native lanes cover planning, architecture, design,
product, copywriting, and research. Pass a stable ID for the current task as
`session_id`, use protocol v2, and report
`surface_capabilities.durable_agent_jobs=true`. Report `native_subagents=true`
only when native agents are actually available. If an older supervisor rejects
v2, retry once with v1 and follow its legacy result. A `native_subagent` response includes
an active, expiring reservation plus a worker profile and model alias; the call
does not itself spawn or control an agent.

Retain the decision ID. Spawn one native worker for the bounded scope, integrate
and verify its result, then call `route_feedback` once with `completed`, `failed`,
`abandoned`, `escalated`, or `not_started`. Identical feedback retries are safe.
On task resume, call `route_reconcile` with that session's decision IDs that are
still running; omitted active reservations are released. Focused native routes
use Terra with high reasoning for Codex and Sonnet for Claude. Capacity
exhaustion returns `direct`, so the primary continues the work itself.

1. Inspect the exact project and define one checkpoint, risk, and expected output.
2. Load only the relevant rubric and incorporate it into `instructions`.
3. Call `route_decide` and follow an enforced lane/provider/model. Submit
   asynchronously with the exact absolute `workdir`. Set
   `context_git_diff=true` for code review and select the correct base ref.
4. Save the job ID, cursor, and exact owner. For MCP, use
   `job_read(wait_seconds=10)` for a short server-side wait until progress or
   terminal state; repeat only after the tool returns. The MCP adapter clamps
   longer requests so a quiet provider cannot outlive a coding surface's socket
   budget. The guarded CLI may use waits up to 60 seconds.
5. Treat `possibly_stalled` as alive but quiet. Cancel only after inspecting status,
   elapsed time, and the run deadline.
6. On failure of an enforced v2 route, report `escalated`, request the one-hop
   route, then submit its returned provider/model as a new job. Record both job
   IDs and both decision IDs. For a shadow route or supervisor outage, submit the
   static-table fallback as a new job instead; no enforceable parent exists.
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

Use `queue_timeout_seconds` for capacity waiting and `run_timeout_seconds` as
the execution backstop. Defaults are 900 and 5400 seconds; queue waiting does
not consume execution time. `timeout_seconds` is a deprecated run-time alias.
Use the shared 5400-second run default for every provider. Do not lower it for a
substantive review merely because the result is expected sooner; choose a shorter
deadline only for a genuinely bounded job with a concrete operational reason.
Provider turn ceilings are retired from the effective ACO contract. New callers
do not send `max_turns`; legacy inputs are accepted but ignored. Jobs run until
their queue/run deadline, cancellation, or
provider termination. An arbitrary cap can discard an otherwise healthy run
after its tokens have already been spent.

## Use the available binding

- **Codex/Hermes with MCP:** call `route_decide`, `route_feedback`,
  `route_reconcile`, `route_status`, `job_submit`, `job_read`, `job_list`,
  `job_cancel`, and `job_inbox` from the `agent-jobs` server.
- **Claude or a shell-only session:** run `scripts/review.py` with the equivalent
  `route-decide`, `route-feedback`, `route-reconcile`, `route-status`, `submit`,
  `read`, `list`, `cancel`, or `inbox` arguments.
- **OpenCode:** call the same tools from the `agent-jobs` MCP server, passing
  `caller_provider=opencode`, `surface=opencode`, and your current model as
  `caller_model` so routing can skip a target from your own model family.

Both review bindings use the same safety core. Explicit implementation goes
directly to the supervisor's capability-gated write path. Read
[operations.md](references/operations.md) for exact CLI examples and recovery.

## Delegate implementation

For explicit substantive delegation, run `scripts/delegate.py` with provider,
model, mode, absolute workdir, and one bounded prompt. `implement` permits scoped
reads and edits but no Bash, Git, external messaging, or nested agents. The calling
agent runs final verification and Git operations afterward. Claude implementation
jobs on macOS are kernel-confined to writes inside the selected workdir plus a
private temporary runtime directory, with Git metadata kept read-only; Codex uses
its native workspace sandbox. Treat this as blast-radius reduction, inspect the
entire diff before executing repository-controlled commands, and repair any CLI
authentication refresh outside the delegated run.
The supervisor fails closed where it cannot enforce an equivalent write boundary.

For Claude implementation jobs, the caller may add repeatable
`--check 'NAME=COMMAND'` arguments for bounded, iterative verification. Codex
check contracts fail closed until an equivalent mediated tool path is verified. These are exact caller-approved argv contracts, not a shell exposed to
the delegated model; the model can only call `run_check(NAME)`.
Prefer focused tests, linters, type checks, or builds. Never approve package
installation, Git, deployment, dev servers, secret-dependent commands, or other
long-lived/external side effects. Approved checks run without provider credentials
or network access and with bounded time/output under the implementation sandbox.
Because a delegated job may edit project code before invoking a check, approving
the check explicitly authorizes execution of model-influenced repository code.

OpenCode submissions may omit `model` or pass `default`; the supervisor then
selects `AGENT_JOB_OPENCODE_DEFAULT_MODEL`. OpenCode is read-only, and explicit
models must use an allowed provider prefix (`opencode-go/` by default) so jobs
never draw pay-as-you-go credits. An explicit OpenCode model from the caller's own
family, including a `default` that resolves to one, routes `direct`, because it
would not be a cross-family review.

Explicit Claude and Codex jobs still require a model.

Use these canonical model aliases:

- Claude `opus`: architecture, UI/UX, visual design, product judgment, copywriting.
- Claude `sonnet`: ordinary implementation when Claude is explicitly requested.
- Claude `fable`: only when explicitly requested or its capability fits the task.
- OpenCode `default`: the configured Go model (`opencode-go/muse-spark-1.3-contributor`)
  for cross-family review, planning, and research consultation.
- OpenCode `opencode-go/kimi-k3`: Kimi K3 on Go; its allowance is small, so request
  it only when K3 specifically matters.
- Codex: pass the currently configured Codex model when another caller requests it.

After completion, inspect the complete diff, reject unrelated changes, run focused
and end-to-end verification, update durable documentation/session logs, and own
the final result.
