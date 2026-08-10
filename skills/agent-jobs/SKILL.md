---
name: agent-jobs
description: Route durable cross-agent reviews, consultations, planning, design, copywriting, research, or explicitly delegated implementation among Codex, Claude, and Kimi. Use when an independent model can materially improve a checkpoint or when the user explicitly asks one provider to perform scoped work. The skill owns routing and review policy; the generic job tools only submit, observe, list, cancel, and deliver durable job notices.
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

Default routing by caller:

| Caller | Code review | Planning, design, product, copy, research |
|---|---|---|
| Codex or Hermes | Kimi K3, then Opus on provider failure | Opus, then Kimi K3 on provider failure |
| Claude | Codex, then Kimi K3 on provider failure | Codex, then Kimi K3 on provider failure |
| Kimi | Codex, then Opus on provider failure | Opus, then Codex on provider failure |

An explicit user model/provider request overrides these defaults. A fallback is
for provider failure, quota exhaustion, or unusable output, not disagreement.
Do not routinely call both providers.

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

- **Codex/Hermes with MCP:** call `job_submit`, `job_read`, `job_list`,
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

Use model aliases so provider upgrades resolve without skill edits:

- Claude `opus`: architecture, UI/UX, visual design, product judgment, copywriting.
- Claude `sonnet`: ordinary implementation when Claude is explicitly requested.
- Claude `fable`: only when explicitly requested or its capability fits the task.
- Kimi `kimi-code/k3`: code-heavy review or implementation.
- Codex: pass the currently configured Codex model when another caller requests it.

After completion, inspect the complete diff, reject unrelated changes, run focused
and end-to-end verification, update durable documentation/session logs, and own
the final result.
