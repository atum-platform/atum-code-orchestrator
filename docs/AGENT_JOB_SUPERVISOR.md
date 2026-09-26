# Agent Job Supervisor

The agent job supervisor owns long-running Claude Code, Codex, and OpenCode CLI
processes independently of the Codex, Claude, OpenCode, or Hermes session that
submitted them. It replaces caller-bound subprocess waits with durable job IDs.

Kimi was removed as a provider on 2026-09-26. Kimi K3 is reached through
OpenCode Go instead; see [OpenCode provider](#opencode-provider).

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

The local SQLite queue uses WAL mode with `synchronous=NORMAL` and bounded
automatic checkpoints. This keeps provider output persistence from blocking the
supervisor's Unix-socket control plane on slow or pressured local storage. SQLite
consistency and ordinary process/restart durability are preserved; a sudden
power loss may discard the newest uncheckpointed queue updates, whose provider
processes are reconciled on the next supervisor start.

Legacy `review-sidecars` and `claude-plan` registrations are migration inputs
only. This standalone repository does not ship those mode-heavy MCP servers.

## Lifecycle

1. A caller submits `provider`, `model` (optional for OpenCode), `mode`, `workdir`, `prompt`,
   an idempotency key, and independent queue and run timeouts over the user-only
   Unix socket.
2. The daemon validates the workdir, model, prompt size, recursion depth, and
   provider, then persists a queued job in SQLite before returning its ID. A
   blank or `default` OpenCode model resolves to
   `AGENT_JOB_OPENCODE_DEFAULT_MODEL`. Jobs retain `requested_model` alongside
   the effective `model` for auditability. Claude and Codex still require an
   explicit model.
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
7. On daemon restart, previously running jobs are marked `interrupted` before the
   socket is bound, so no caller can observe a job the daemon no longer owns.
   Their orphaned process groups are then reaped behind the bound socket, in
   parallel, because each costs two `ps` probes and a grace period; startup
   latency no longer scales with the number of interrupted jobs. A process group
   is terminated only when PID, PGID, and process start time all exactly match
   the recorded identity. The recorded executable (`binary_path`) is diagnostic,
   not part of the match: a launch that passes through `sandbox-exec` or a script
   can be probed before it `exec`s the provider.
8. Every terminal transition with a non-empty owner creates one durable inbox
   delivery. Reads redeliver until that exact owner acknowledges it.

New jobs default to a 15-minute `queue_timeout_seconds` budget measured from
submission and a 90-minute `run_timeout_seconds` budget measured from provider
launch. Both accept 30 seconds through two hours. The deprecated
`timeout_seconds` input remains an alias for the run budget. Existing rows that
predate the split retain their original submit-relative shared deadline and
report `timeout_semantics=legacy_shared`; new rows report `separate` plus
`queue_deadline_at` and, after launch, `run_deadline_at`. A job still queued
for a provider that a later release removed fails at once with `launch_error`
rather than holding up the jobs queued behind it.

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

### Claude runtime and credentials

The supervisor resolves the Claude binary at every submission and launch:
`AGENT_JOB_CLAUDE_BIN` when it names an executable, then the newest Claude
Desktop bundled release under
`~/Library/Application Support/Claude/claude-code/<version>/`, then `claude` on
the service `PATH` and the known install locations. Releases compare
numerically, and a release directory without its binary (an update still
unpacking) is skipped. The desktop app installs each release in a new versioned
directory and later prunes old ones, so never pin a versioned path.

The installer persists `AGENT_JOB_CLAUDE_BIN` only when it is set on the install
command. Earlier installers wrote a pin unconditionally, so a retained value is
dropped rather than trusted. When that retained launcher is a script, the
installer refuses until `AGENT_JOB_PROFILE_ENV` names an existing file, because
the launcher scripts seen so far (an untracked checkout wrapper and a
`~/.local/bin/claude` shim) were what supplied Claude's credentials. Set
`AGENT_JOB_CLAUDE_BIN` on the install command to keep a script deliberately.

Supply credentials through `AGENT_JOB_PROFILE_ENV`, not a launcher wrapper.
Claude jobs receive only `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
`ANTHROPIC_TOKEN`, and `CLAUDE_CODE_OAUTH_TOKEN` from it, and OpenCode jobs only
`OPENCODE_API_KEY`. Launching the real binary also keeps each job's `binary_path`
accurate; through a wrapper the probe usually recorded the transient shell
(`/bash`) rather than the Claude release that ran.

### OpenCode provider

OpenCode replaces Kimi as the default review target. Jobs are read-only:
`implement` jobs and the CAO backend are refused at submission, and a workdir
outside a Git work tree fails at launch.

- **CLI contract.** Only the verified 1.x `opencode run --format json` interface
  is accepted, checked with `--version` per resolved binary. The binary resolves
  per launch from `AGENT_JOB_OPENCODE_BIN`, then `PATH` (Homebrew's
  `anomalyco/tap/opencode`), then known locations; the installer never pins the
  Homebrew link's versioned Cellar target. OpenCode Desktop bundles a 2.x CLI
  with a different contract (`--standalone`, no `--pure`), which is refused.
- **Sealed copy.** Each job runs in `runtime/<job>/workspace`, a copy-on-write
  clone of the workdir's Git-visible files (`git ls-files --cached --others
  --exclude-standard`). It omits `opencode.json`, `opencode.jsonc`, and
  `.opencode/`, from which OpenCode loads plugins and MCP servers without a trust
  prompt; `.env` and `.envrc` files other than examples, samples, and templates;
  key, keystore, credential, and Terraform variable or state files; `.kube`,
  `.docker`, and other secret-store directories; and symlinks. The copy is not a Git
  repository, so OpenCode's config discovery stops at its root. At most 50,000
  files are staged. Staging and teardown run on worker threads so a large
  repository cannot stall the control socket, and launch refuses to fall back to
  the real workdir if the copy is missing.
- **Private home.** `HOME`, `TMPDIR`, and all four XDG directories point into
  `runtime/<job>/opencode-home`, so no user config, plugins, sessions, or
  `~/.claude` guidance load, and temporary tool output is deleted with the job.
  `OPENCODE_DISABLE_CLAUDE_CODE`, default plugins, LSP downloads, and auto-update
  are off, `--pure` skips external plugins, and language servers and formatters
  are disabled because they can start repository-local binaries.
- **Permissions.** `OPENCODE_PERMISSION` and the job's `aco-review` agent deny
  everything except read, glob, grep, and list, and deny reads of `.env` files
  other than examples, samples, and templates.
  OpenCode applies the last matching rule, and no rule is `ask`; headless `run`
  rejects any permission request it receives regardless.
- **Credentials.** `OPENCODE_API_KEY` comes from `AGENT_JOB_PROFILE_ENV`, which
  accepts an `os.pathsep`-separated list of env files where later files win. Each
  provider receives only its own keys.
- **Models and billing.** `default` resolves to `AGENT_JOB_OPENCODE_DEFAULT_MODEL`
  (`opencode-go/muse-spark-1.3-contributor`). Every model must match
  `AGENT_JOB_OPENCODE_MODEL_PREFIXES` (`opencode-go/`), because the same key could
  otherwise draw pay-as-you-go Zen credits. Only the providers those prefixes
  name are enabled, the session title is fixed with `--title`, and
  `small_model` is pinned to the job's model, because titles and compaction
  otherwise call a separate small model. A default from the OpenAI or
  Anthropic family is refused so default reviews stay cross-family, and routing
  sends an explicit OpenCode model from the caller's own family `direct`,
  resolving a requested `default` to the configured model first. Muse
  Spark Contributor trains on prompts and completions, so Go serves it only after
  the workspace's Privacy settings allow paid endpoints that train on request
  data; otherwise the job fails with an upstream error naming that setting. Until
  then set `AGENT_JOB_OPENCODE_DEFAULT_MODEL=opencode-go/kimi-k3`, which does not
  train. Go includes US$60 a month of Muse Spark and US$15 a month of Kimi K3 at
  list prices.
- **Errors.** Failures arrive as JSON `error` events on stdout with exit code 1.
  Jobs also pass `--print-logs --log-level ERROR`, because the JSON event can
  reduce the cause to "Unexpected server error". Only error-event text, never
  tool output, is scanned for rate limits, which record the usual routing
  cooldown. The prompt is written to stdin, which `run` reads to end of file;
  launching with an open stdin would hang.
- **No anonymous free tier.** OpenCode's anonymous free models serve only its
  interactive apps; headless `run` receives HTTP 403 `FreeTierError` from both
  CLI generations. ACO requires a Go or Zen API key and never impersonates
  another client.

### Service restart policy

`KeepAlive` is unconditionally true. A supervisor that is signalled exits zero,
and the earlier `{"SuccessfulExit": false}` setting told launchd to treat that as
intentional and leave the service down until somebody restarted it by hand. Any
change that makes a clean exit mean "stay down" reintroduces the 2026-09-04
outage, so the installer asserts this in tests.

`ExitTimeOut` is 30 seconds and must stay above the daemon's own
`AGENT_JOB_SHUTDOWN_GRACE_SECONDS` (15 by default), which bounds how long
shutdown waits for provider children. Below it, launchd sends SIGKILL part-way
through shutdown. A lock conflict exits 75 and logs
`supervisor_already_running`; with `KeepAlive` on, launchd retries it every
`ThrottleInterval`, which is the intended takeover behaviour when a previous
instance is still releasing the state directory.

The daemon writes one JSON lifecycle line per start and stop to
`supervisor.stderr.log` (`supervisor_started` with the reconciled job count,
`supervisor_stopping`, `supervisor_shutdown_timeout`, `supervisor_stopped`). These
lines are the only durable evidence of a restart: launchd does not persist
LaunchAgent exit records at any level the unified log retains.

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
OpenCode's omitted-model default is `opencode-go/muse-spark-1.3-contributor`;
deployments override it with `AGENT_JOB_OPENCODE_DEFAULT_MODEL`. Approved
workspace roots are defined once in `tools/agent_job_policy.py` and used by the
installer, supervisor, and review core. Hermes-owned paths are intentionally
excluded. Override the roots consistently with `AGENT_JOB_ALLOWED_ROOTS` when
deploying elsewhere.

The same socket accepts protocol-v1 and protocol-v2 `route_decide` requests plus
`route_feedback`, `route_reconcile`, and `route_status`. V1 preserves the legacy
model aliases and assumes durable jobs are available. V2 intersects declared
client capabilities with the server-owned surface matrix and returns exact
Codex/Claude/OpenCode model IDs. Caller/surface mismatches fail closed in both
versions. A lane the client cannot execute degrades explicitly to
`direct`; unsupported native claims never create reservations.

Shadow mode validates and records
centralized recommendations without changing caller behavior. Set
`AGENT_JOB_ROUTING_MODE=codex_canary` to make only Codex-on-Codex responses
authoritative. `surface_canary` also makes v2 decisions authoritative for Codex,
Claude Code/Desktop, and OpenCode while keeping every v1 caller in shadow. An
OpenCode caller may pass `caller_model` so routing skips the target that shares
its model's family. Eligible focused same-family work from Codex or Claude Code
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
(default 900, bounded to 30-86400). The client installer declares a `codex-worker`
Codex role backed by `clients/codex/codex-worker.toml`; Claude Code uses its
native general-purpose worker interface. Focused native routing uses GPT-5.6
Terra with high reasoning for Codex and Sonnet for Claude. The installer
sets the stable Codex `agents.max_threads` machine ceiling to three when the user
has not already chosen one. Feedback is idempotent, reconciliation is
session-scoped, and status reports
reservation counts plus the terminal decision-to-feedback return rate. Expired
or reconciled decisions without feedback intentionally lower that rate because
it measures whether callers returned, not transport delivery reliability. Both
jobs and inactive route decisions use the configured retention window; active
reservations are retained until feedback, reconciliation, or TTL expiry.

The Codex route returns both the Terra model alias and the `codex-worker`
profile. The profile carries `model_reasoning_effort = "high"`; cooperating
Codex callers must spawn the returned profile rather than reconstructing a
worker from the model alias alone.

### Quota broker

Set `AGENT_JOB_QUOTA_ROUTING=1` to let the supervisor rebalance default
`agent_jobs` routes. The broker reads `claude.json`, `codex.json`, and
`opencodego.json` (for the `opencode` provider) from CodexBar's local history
directory. Override the directory with
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

Native Codex, Claude, and OpenCode jobs produce schema-v1 records in
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
Malformed Claude and OpenCode records retain only byte count and digest.
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
On macOS, native Claude implementation processes run under a Seatbelt
profile that permits writes only in the resolved submitted workspace and a
private per-job runtime directory, except that workspace Git metadata remains
read-only. Symlink-resolved writes outside those paths are denied by the kernel.
The runtime directory is mode `0700`, becomes the provider's `TMPDIR`, and is
removed after normal termination or on the next supervisor start. Codex continues to use
its native `workspace-write` sandbox. If the required platform sandbox is not
available, implementation fails closed. The optional CAO backend is rejected for
implementation until it can provide the same enforceable contract.

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
attaching up to eight named approved checks. Codex check contracts fail
closed until equivalent tool mediation is verified for that provider CLI. The
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

Reads advance the normalized stream with the opaque byte `event_cursor`. On
terminal failure, cancellation, or interruption, `partial_response` and
`partial_result_state` make retained work recoverable. Existing callers that
omit `event_cursor` keep their prior log-only behavior for non-semantic
providers; native Claude and OpenCode callers consume events and
`partial_response` rather than raw stream JSON. OpenCode keeps output-byte
liveness because its JSON stream reports tools only when they finish. Partial states are
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
  `failure_kind` distinguishes the two: `supervisor_shutdown` is written by the
  daemon that owned the job as it stops, `supervisor_restart` by the next daemon
  for jobs whose owner died without recording anything.

A job's provider process does not survive a daemon restart: it is a child of the
daemon and is reaped on the way down, or identity-checked and killed by the next
startup. The durable guarantee is the record, not the process. Every in-flight
job therefore receives its terminal row in one commit before shutdown does any
slow work, so a caller polling across a restart always sees an outcome instead of
a job that no longer exists. Queued jobs are unaffected and run once a daemon is
back.

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
Native Claude and OpenCode stdout is retained only in the mode-`0600` raw file for
local diagnostics; ordinary reads do not expose it or mirror it into the
combined log. Each job persists its `semantic_stream` selection at submission,
so later configuration changes never reinterpret retained or already queued
jobs and cannot expose their structured stdout, even after a later release
removes the job's provider. Only Codex's JSON stdout is public. CAO-bridged
jobs have no
semantic adapter; their stdout stays readable as plain output.

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
