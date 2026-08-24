# Agent Job Supervisor

The agent job supervisor owns long-running Claude Code, Codex, and Kimi Code CLI
processes independently of the Codex, Claude, or Hermes session that submitted
them. It replaces caller-bound subprocess waits with durable job IDs.

Kimi execution negotiates the installed CLI contract at launch. The legacy
Python CLI uses YAML agents, print-mode JSON streaming, and an explicit empty
MCP file. The current Node CLI uses Markdown agents, prompt-mode JSON streaming,
an isolated per-job `KIMI_CODE_HOME`, and an empty Skills directory. This keeps
automatic Kimi upgrades from silently leaving the supervisor on retired flags.

## Architecture

The supported interface follows a fat-skill, thin-harness split:

- `skills/agent-jobs/` owns provider routing, review rubrics, fallback policy,
  polling judgment, and explicit implementation delegation.
- `tools/review_core.py` owns non-negotiable read-only enforcement, workspace and
  context containment, secret refusal/redaction, bounded Git context, and the
  mapping to supervisor jobs.
- `tools/agent_jobs_server.py` and `tools/review_cli.py` are equivalent bindings
  over that core. The MCP server exposes guarded submit, read, list, cancel, and
  owner-inbox operations; it accepts typed instructions rather than a raw prompt
  and cannot select write mode.
- `tools/agent_job_supervisor.py` owns process lifecycle, persistence,
  credentials, deadlines, concurrency, and capability-gated implementation.

Legacy `review-sidecars` and `claude-plan` registrations are migration inputs
only. This standalone repository does not ship those mode-heavy MCP servers.

## Lifecycle

1. A caller submits `provider`, optional Kimi `model`, `mode`, `workdir`, `prompt`,
   an idempotency key, and independent queue and run timeouts over the user-only
   Unix socket.
2. The daemon validates the workdir, model, prompt size, recursion depth, and
   provider, then persists a queued job in SQLite before returning its ID. A
   blank Kimi model defaults to `kimi-code/k3`; supported aliases are
   canonicalized, while stale or unknown Kimi aliases fall back to
   `kimi-code/kimi-for-coding` (K2.7). Jobs retain `requested_model` alongside
   the effective `model` for auditability and expose a message when an unknown
   or legacy alias falls back. Claude and Codex still require an explicit model.
3. A machine-wide provider queue atomically claims the job as `launching`, then
   launches it once in a new process group.
4. Output is appended to a cursor log and to separate raw stdout/stderr files.
   Native Codex and Claude JSONL is also normalized into a bounded event journal
   before the human-readable log prefix is added.
5. `read` reports lifecycle state, semantic activity, new output, normalized
   events, silence duration, and terminal output. A bounded wait is held
   server-side and wakes without repeated client sockets.
6. Cancellation sends `SIGTERM` to the process group, waits ten seconds, then
   sends `SIGKILL` if necessary.
7. On daemon restart, previously running jobs are marked `interrupted`. A process
   group is terminated only when PID, PGID, process start time, and resolved
   executable all exactly match the recorded identity.
8. Every terminal transition with a non-empty owner creates one durable inbox
   delivery. Reads redeliver until that exact owner acknowledges it.

New jobs default to a 15-minute `queue_timeout_seconds` budget measured from
submission and a 45-minute `run_timeout_seconds` budget measured from provider
launch. Both accept 30 seconds through two hours. The deprecated
`timeout_seconds` input remains an alias for the run budget. Existing rows that
predate the split retain their original submit-relative shared deadline and
report `timeout_semantics=legacy_shared`; new rows report `separate` plus
`queue_deadline_at` and, after launch, `run_deadline_at`.

Silence does not automatically kill a job. `lifecycle_status` is the persisted
authority; `activity` reports `starting`, `streaming`, `reasoning`,
`tool_running:<name>`, `waiting_on_provider`, `idle_unknown`, or `terminal`.
`open_tool_count` reports concurrent top-level tools while `open_tool` remains
the oldest tool name. Provider-declared waiting is bounded by the same soft
silence threshold and becomes `idle_unknown` if no further progress arrives.
For compatibility, `status` can still report `possibly_stalled` while the
persisted lifecycle remains `running`. Only cancellation or the applicable
queue/run deadline terminates work. Queue time never consumes a new job's run
budget.

## Installation

```bash
python3 bootstrap.py
.venv/bin/python tools/agent_job_client.py ping
```

The LaunchAgent label is `com.atum.agent-job-supervisor`. Runtime state is kept
under `~/.local/state/agent-job-supervisor` with user-only permissions.
The Hermes cluster uses a different checkout, LaunchAgent label, and state
directory; ACO installation does not manage it.

## Operations

```bash
python3 tools/agent_job_client.py list
python3 tools/agent_job_client.py read JOB_ID --cursor 0 --event-cursor 0
python3 tools/agent_job_client.py cancel JOB_ID
python3 tools/install_agent_job_supervisor.py status
```

Durable provider concurrency defaults to a ceiling of three jobs per provider.
Override with `AGENT_JOB_<PROVIDER>_CONCURRENCY`; integer values are clamped to
the one-to-three supported range. Set `AGENT_JOB_DYNAMIC_CONCURRENCY=1` alongside
quota routing to reduce a pressured provider by one slot and pause new launches
while that provider is in a canonical rate-limit cooldown. Running jobs are
never cancelled when capacity falls, and missing or stale quota telemetry keeps
the configured ceiling. Cooldown expiry restores at least one slot; pressure
hysteresis may keep the provider one slot below its ceiling until pressure falls
below 70%. `route_status` exposes configured and effective slots.

Native-agent reservations remain fixed and advisory. The status response reports
whether their decision-to-feedback join rate has reached the 95% prerequisite
for any later dynamic enforcement; P3 does not change native capacity. Approved
Kimi's omitted-model default is `kimi-code/k3`; deployments may override it with
`AGENT_JOB_KIMI_DEFAULT_MODEL` after confirming the target machine's Kimi Code
configuration supports that canonical model ID. Approved
workspace roots are defined once in `tools/agent_job_policy.py` and used by the
installer, supervisor, and review core. Hermes-owned paths are intentionally
excluded. Override the roots consistently with `AGENT_JOB_ALLOWED_ROOTS` when
deploying elsewhere.

The same socket accepts protocol-v1 and protocol-v2 `route_decide` requests plus
`route_feedback`, `route_reconcile`, and `route_status`. V1 preserves the legacy
model aliases and assumes durable jobs are available. V2 intersects declared
client capabilities with the server-owned surface matrix and returns exact
Codex/Claude/Kimi model IDs. Caller/surface mismatches fail closed in both
versions. A lane the client cannot execute degrades explicitly to
`direct`; unsupported native claims never create reservations.

Shadow mode validates and records
centralized recommendations without changing caller behavior. Set
`AGENT_JOB_ROUTING_MODE=codex_canary` to make only Codex-on-Codex responses
authoritative. `surface_canary` also makes v2 decisions authoritative for Codex,
Claude Code/Desktop, and Kimi Code while keeping every v1 caller in shadow.
Eligible focused same-family work from Codex, Claude Code, or Kimi Code
atomically claims an expiring cooperative native reservation; the supervisor
does not spawn or terminate the subagent and does not change durable `submit`
behavior.
Unknown routing modes fail during supervisor startup.

Routing identity and capabilities are self-asserted by clients on a trusted
per-user Unix socket; they coordinate cooperating processes and are not an
authentication boundary against another process running as the same user.

`AGENT_JOB_NATIVE_RESERVATIONS` controls the machine-wide cooperative reservation
limit shared by all coding surfaces (default 3). The legacy
`AGENT_JOB_CODEX_NATIVE_RESERVATIONS` name remains an accepted fallback during
the compatibility window. `AGENT_JOB_ROUTE_RESERVATION_SECONDS` controls TTL
(default 900, bounded to 30-86400). The client installer declares a `spark-worker`
Codex role backed by `clients/codex/spark-worker.toml`; Claude Code and Kimi Code
use their native general-purpose worker interfaces. Focused native routing uses
Spark for Codex, Sonnet for Claude, and high-speed K2.7 for Kimi. The installer
sets the stable Codex `agents.max_threads` machine ceiling to three when the user
has not already chosen one. Feedback is idempotent, reconciliation is
session-scoped, and status reports
reservation counts plus the terminal decision-to-feedback return rate. Expired
or reconciled decisions without feedback intentionally lower that rate because
it measures whether callers returned, not transport delivery reliability. Both
jobs and inactive route decisions use the configured retention window; active
reservations are retained until feedback, reconciliation, or TTL expiry.

### Quota broker

Set `AGENT_JOB_QUOTA_ROUTING=1` to let the supervisor rebalance default
`agent_jobs` routes. The broker reads `claude.json`, `codex.json`, and any future
`kimi.json` from CodexBar's local history directory. Override the directory with
`AGENT_JOB_QUOTA_HISTORY_DIR`; no browser cookies, provider credentials, or
CodexBar process access are required.

For every active quota window, pressure is the greater of current utilization
and utilization projected linearly to the reset, capped at 100%. A provider
enters `pressured` at 85% and leaves only below 70%, providing hysteresis across
samples. Telemetry older than two hours (`AGENT_JOB_QUOTA_STALE_SECONDS`) is
`stale`; missing telemetry is `unknown`. An expired quota window is also stale
until a post-reset sample arrives. Stale or missing evidence for the primary
provider preserves static routing and emits an alert rather than inventing
pressure. A known-pressured primary may still move to an unknown fallback, but
never to a rate-limited fallback or an equally/more pressured fallback. A
rate-limited primary may use a pressured fallback because it cannot serve the
request itself.

Actual utilization at or above 98% is a separate `exhausted` state, with
hysteretic recovery at 95% or below. Exhaustion excludes the provider from
automatic default, fallback, escalation, and native-worker routing. An explicit
provider request is an operator override and remains executable; status still
reports the exhaustion so the caller can warn accurately.
Projected pressure alone remains a balancing signal and does not trigger this
hard boundary. Temporary rate-limit cooldowns continue to queue already chosen
work for automatic recovery.

With quota routing enabled, nonzero provider exits containing a bounded,
provider-specific rate-limit signature in stderr are
normalized to `failure_kind=rate_limit` and persisted in the health ledger. A
nearby reset interval sets the cooldown; otherwise the default is
15 minutes (`AGENT_JOB_RATE_LIMIT_COOLDOWN_SECONDS`). Repeated failures can
extend but never shorten a cooldown. Once it expires, the next route/status
refresh automatically reconsiders the provider, so callers do not permanently
abandon a recovered model.

Only default routes are rebalanced. Explicit user provider/model choices remain
authoritative, recursive delegation remains forbidden, and a rate-limited
fallback is never selected. `route_status` exposes the feature flag, provider
states, pressure, reset/cooldown timestamps, telemetry source, and alerts.
Disable `AGENT_JOB_QUOTA_ROUTING` for immediate policy rollback without deleting
prior health evidence or changing queued jobs. Disabled mode performs no quota
cache reads, health writes, route changes, or failure-kind normalization.

### One-hop escalation

Protocol v2 supports one terminal retry through the routing layer. A caller must
first mark an enforced parent decision `escalated`, then submit the same caller,
surface, and session identity with the parent ID, a typed reason, and bounded
non-secret evidence. Parent validation, child uniqueness, and persistence run in
one SQLite `BEGIN IMMEDIATE` transaction. An identical request recovers the same
child; a different child request and any child-of-child request fail closed.

The supervisor computes the ordinary route, applies quota/cooldown rebalancing,
and then excludes the provider used by the parent. A fallback that is not in an
active rate-limit cooldown becomes the terminal provider; stale, missing, and
pressured quota telemetry retain the system-wide fail-open routing semantics.
An actively rate-limited fallback degrades to direct execution by the primary
agent. Native-worker escalation also degrades to direct
execution because recursively spawning the same worker family would not change
the failure boundary. Every child clears its fallback fields. `route_status`
reports child counts by escalation reason, while the SQLite record retains the
bounded evidence for diagnosis.

CAO migration is provider-scoped. Keep `AGENT_JOB_EXECUTION_BACKEND=native`,
then set both `AGENT_JOB_CAO_CANARY_PROVIDERS` and
`AGENT_JOB_CAO_CANARY_OWNER_PREFIXES` to route only matching provider/owner
pairs. After the evidence gate passes, move a provider to
`AGENT_JOB_CAO_PROVIDERS`. These settings and CAO connection settings are
forwarded by the LaunchAgent installer; reinstall and restart the service after
changing them. Backend selection is persisted at submission, so rollback does
not rewrite queued or running jobs.

Durable `implement` mode requires both the installed service policy and a random
capability stored in `~/.local/state/agent-job-supervisor/implement.token` with
mode `0600`. The installer enables this policy for the scoped delegation client;
the token prevents accidental or malformed write submissions but is not a
privilege boundary against other processes running as the same macOS user.
The daemon scopes provider API credentials at process launch from its environment
or `AGENT_JOB_PROFILE_ENV`; it never stores credential values in SQLite.

Native Codex, Claude, and Kimi jobs produce schema-v1 records in
`<job>.log.events.jsonl` and assemble assistant message events into
`<job>.log.partial.txt`. Claude runs with `stream-json`, partial messages,
verbose events, and session persistence disabled. Its partial response is all
top-level assistant-visible text in order. Assistant snapshots are reconciled
against streamed prefixes without inferring provider block indices. A successful,
top-level terminal `result.result` is recovered only when no answer text was
otherwise emitted, and records a `terminal_result_recovered` progress marker;
error, nested, non-string, and duplicate terminal results never inject text.
Subagent events are not appended. Tool arguments, tool-result content, thinking
signatures, machine inventories, and account utilization stay out of the
normalized journal. Decoder state is process-local and is never replayed into an
existing partial-response file after restart. If terminal answer size indicates
possible mixed response loss, the result is marked partial and delegation clients
print a warning without exposing the omitted terminal text. Claude stream-prefix
tracking is bounded to 256 blocks and 1 MiB per block; exceeding either bound
suppresses snapshot recovery for that message to avoid duplicate answer text.
Kimi runs with
`stream-json`; its assistant records are incremental message chunks, while tool
calls and results are reduced to names, IDs, and byte counts. Malformed Claude
and Kimi records retain only byte count and digest.
Raw bounded logs remain private operational evidence under the user-only state
directory and are not returned through normal semantic job reads.

Claude's native-backend tool surface is selected with `--tools`, not merely
approved with `--allowed-tools`. Read-only jobs expose only `Read`, `Glob`, and
`Grep`; implementation jobs add `Edit` and `Write`. Both modes deny the shell
tool family, current and legacy subagent tools, workflow, network, and notebook
tools as defense in depth. Both also use an empty strict MCP configuration and
safe mode, so project or user hooks and other customizations cannot introduce a
separate execution path. This prevents within-session shell execution and nested
delegation at the native Claude CLI boundary. It does not by itself confine
absolute paths, so the supervisor adds a second boundary for implementation.
On macOS, native Claude and Kimi implementation processes run under a Seatbelt
profile that permits writes only in the resolved submitted workspace and a
private per-job runtime directory, except that workspace Git metadata remains
read-only. Symlink-resolved writes outside those paths are denied by the kernel.
The runtime directory is mode `0700`, becomes the provider's `TMPDIR`, and is
removed after normal termination or on the next supervisor start. Codex continues to use
its native `workspace-write` sandbox. If the required platform sandbox is not
available, implementation fails closed. The optional CAO backend is rejected for
implementation until it can provide the same enforceable contract.

Kimi implementation jobs set `KIMI_SHARE_DIR` to a disposable directory under
the per-job runtime so logs and sessions do not require writes to `~/.kimi`.
The real config and credentials remain readable for subscription authentication
but are not writable through the sandbox; token refresh that requires durable
credential rotation therefore fails closed and must be repaired outside the job.

This checkpoint reduces delegated write blast radius; it is not a complete
security boundary. A provider still has network access for inference and can
read files available to the macOS user, while workspace-controlled settings may
still affect commands the calling agent runs later. The calling agent must inspect
the complete diff before running commands. Subscription CLIs may also fail closed
if they try to refresh durable authentication state during an implementation job;
refresh or repair authentication outside the delegated run.
The no-shell/no-network tool
surface, safe mode, empty MCP configuration, secret-context rejection, and
workspace policy remain the read-side controls. Full process-level read
isolation would require provider authentication to be injected into a disposable
home rather than read from each CLI's durable local login.

Claude implementation callers can opt into narrowly mediated verification by
attaching up to eight named approved checks. Codex and Kimi check contracts fail
closed until equivalent tool mediation is verified for those provider CLIs. The
supervisor persists each exact argv only
until provider launch, injects one private `aco_checks.run_check(name)` MCP tool,
and clears the contract from durable job metadata after launch. The delegated
model supplies only the name; it cannot supply or alter command text. Each check
runs serially without a shell added by ACO, without provider credentials or proxy
variables, with network denied, Git metadata read-only, workspace/sibling write
confinement, a maximum 15-minute deadline, bounded captured output, and process
cleanup. The caller may explicitly approve an argv that invokes a project script
or shell, so the trust decision remains with the caller. Repository code becomes
model-influenced as soon as the delegated job edits it; approving `npm test`,
`pytest`, or a similar command therefore authorizes execution of code the model
may have changed. Do not approve package installation, Git, deployment, dev
servers, commands requiring secrets, or untrusted code. The macOS profile is
targeted blast-radius reduction, not a default-deny execution sandbox: it blocks
network, Apple Events, common launchd/script escapes, sensitive credential reads,
out-of-workspace writes, and Git writes, but callers must still inspect the diff.
Kimi always receives an explicit MCP config that replaces its user-level
`~/.kimi-code/mcp.json` registration; its normal subscription config remains
available for authentication and model selection.

Reads advance the normalized stream with the opaque byte `event_cursor`. On
terminal failure, cancellation, or interruption, `partial_response` and
`partial_result_state` make retained work recoverable. Existing callers that
omit `event_cursor` keep their prior log-only behavior for non-semantic
providers; native Claude and Kimi callers consume events and `partial_response`
rather than raw stream JSON. Kimi deliberately keeps output-byte liveness even
with its structured adapter because its JSON stream has no tool-start boundary;
stderr tool progress therefore prevents false stalls during long tools. Partial states are
`complete`, `partial`, `truncated`, `none`, or `unavailable`; the last value
means the selected provider/backend does not have a semantic response adapter.

## Failure Semantics

- `queued`: persisted and waiting for a provider slot.
- `launching`: atomically claimed by the scheduler; provider identity is being
  recorded before the job becomes `running`.
- `running`: persisted lifecycle state for an active daemon-owned process.
- `possibly_stalled`: compatibility status alias when semantic progress is quiet
  past the threshold and no tool is open. This is diagnostic, not terminal;
  inspect `lifecycle_status`, `activity`, and `seconds_without_progress`.
- `completed`: provider exited zero.
- `failed`: queue timeout, launch error, provider non-zero exit, or run timeout.
- `cancelled`: caller requested cancellation.
- `interrupted`: the supervisor stopped or restarted during execution.

The SQLite database contains prompts only while jobs are queued; prompts are
cleared after provider launch and on every terminal path. Paths and hashes remain
for operations and idempotency. Its directory and files are mode `0700`/`0600`.
Never submit secrets, `.env` contents, credentials, or unrelated private data.
Implementation agents cannot run arbitrary Bash or Git. They may run only caller-
approved named checks through the mediated broker; the calling agent remains
responsible for inspecting the diff and running final verification.
Combined and raw per-job logs share a total 10 MiB budget. Normalized event
journals default to 2 MiB and partial responses to 256 KiB. Override these with
`AGENT_JOB_MAX_LOG_BYTES`, `AGENT_JOB_MAX_EVENT_BYTES`, and
`AGENT_JOB_MAX_PARTIAL_RESPONSE_BYTES`. Terminal jobs and all associated files
are retained for 14 days by default; `AGENT_JOB_RETENTION_SECONDS` changes that
window. A state-directory lock prevents a second daemon from competing for the
same queue.

Every normalized payload has an aggregate record bound. The reader also skips
and reports an oversized or corrupt record while advancing its cursor, so damaged
journal data cannot wedge later reads. `journal_truncated` remains set after the
journal reaches its byte budget. A normalization/storage failure disables
semantic decoding for that job but raw stdout drainage and capture continue.
Native Claude and Kimi stdout is retained only in the mode-`0600` raw file for
local diagnostics; ordinary reads do not expose it or mirror it into the
combined log. Set `AGENT_JOB_KIMI_SEMANTIC=0` in the LaunchAgent environment and
restart to restore Kimi's prior text argv, public stdout, and adapter-unavailable
contract for newly submitted jobs as an emergency rollback. Each job persists
its `semantic_stream` selection at submission, so toggling the kill switch never
reinterprets retained or already queued jobs and cannot expose their structured
stdout.

## Verification

```bash
python3 -m unittest discover -s tools/tests -v
python3 -m py_compile tools/agent_job_*.py tools/review_core.py tools/review_cli.py
```

Evaluate one provider after its observation window:

```bash
python3 tools/agent_job_migration_gate.py \
  --provider claude \
  --source-commit CAO_COMMIT \
  --model opus \
  --acceptance-report /tmp/atum-cao-mock.json \
  --acceptance-report /tmp/atum-cao-claude.json \
  --report /tmp/atum-agent-job-claude-gate.json
```
