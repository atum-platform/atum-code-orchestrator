# Agent Jobs Client Integration

The machine-wide agent-job supervisor is a local execution service. Coding
clients join it through the same read-only MCP server or the equivalent review
CLI, while target providers are launched through Claude, Codex, and Kimi CLIs.

## Install

Preview and apply native client configuration:

```bash
python3 tools/install_agent_job_clients.py
python3 tools/install_agent_job_clients.py --apply --backup-suffix YYYYMMDDTHHMMSSZ
python3 tools/install_agent_job_clients.py --check
```

The installer performs structured, idempotent merges and preserves unrelated
settings. Before changing an existing file it creates a sibling
`*.bak.agent-jobs-<suffix>` backup. It manages:

- the shared `~/.agents/skills/agent-jobs` link;
- Claude Desktop's `mcpServers.agent-jobs` registration;
- Kimi Code's user-level `~/.kimi-code/mcp.json` registration; and
- a marked agent-jobs guidance section in `~/.kimi-code/AGENTS.md`.

Restart Claude Desktop and start a new Kimi Code session after applying changes.
Existing sessions retain the tools and instructions loaded when they started.
The installer validates every target and backup slot before writing, and restores
all prior targets if an unexpected later write fails. `--check` exits nonzero
when configuration drift is pending.

To roll back manually, quit the affected client, replace its current config with
the corresponding `*.bak.agent-jobs-<suffix>` file, and restart the client.

## Support Matrix

| Client | Caller binding | Shared policy | Target adapter |
|---|---|---|---|
| Codex Desktop and CLI | MCP | `~/.agents/skills` plus global guidance | Codex CLI |
| Claude Code, including Desktop code sessions | Review CLI | Claude skill link and `CLAUDE.md` | Claude CLI |
| Claude Desktop chat | Local MCP | Tool schema; coding policy applies in Claude Code sessions | Claude CLI |
| Kimi Code | MCP | `~/.agents/skills` and Kimi `AGENTS.md` | Kimi CLI |
| Hermes profiles | MCP | Per-profile skill copies | Not a provider |

MCP intentionally exposes route decision, feedback, reconciliation, status,
submit, read, list, cancel, and owner-inbox operations for read-only jobs.
Explicit implementation remains behind the local capability-protected delegation
CLI. This prevents a general chat client from selecting write mode directly.
Review prompts may contain up to 4 MiB of UTF-8 data. The supervisor's Unix
socket reader is sized for that complete JSON request, so prompts larger than the
former 400 KB ceiling are accepted end to end rather than only by one layer.

## Routing Protocol

`route_decide` supports protocol versions 1 and 2. Both accept a bounded structured intent containing
the caller provider, coding surface, capability, complexity, risk, scope,
duration, durability, parallelizability, optional explicit target, stable session
ID, and a boolean surface-capability map. Unknown fields are tolerated for mixed-version clients
but discarded before persistence;
unknown enum values and unsupported protocol versions fail closed. New clients
send v2 with `durable_agent_jobs=true` and only claim `native_subagents=true`
when that tool is actually present. If an older server rejects v2, retry once
with v1; old clients remain valid against a new server.

The response includes a durable decision ID, policy version, lane, provider,
model alias, worker profile, fallback, effective surface capabilities, reasons,
expiry, and reservation
state. Shadow responses return `enforced=false` and never reserve. With
`AGENT_JOB_ROUTING_MODE=codex_canary`, only a Codex caller on the Codex surface
can receive `enforced=true`; focused session-scoped implementation, exploration,
or test work may receive a `native_subagent` lane. `surface_canary` extends v2
enforcement to the supported Claude and Kimi coding surfaces. V2 selects Opus
for Claude's deep/review/thinking work and K3 for Kimi review or standard/deep
work. Codex targets use concrete GPT-5.6 Sol, with Spark reserved for focused
native work; Fable remains explicit-only.

Native admission and persistence occur in one SQLite `BEGIN IMMEDIATE`
transaction. The default machine-wide cooperative limit is three active Codex
native reservations, each expiring after 900 seconds. Capacity exhaustion
returns `direct`. The reservation is advisory outside cooperating clients: Codex
itself enforces the installed three-thread machine ceiling and owns spawn,
termination, integration, and verification.

```bash
python3 tools/review_cli.py route-decide \
  --caller-provider codex --surface codex --capability planning \
  --complexity deep --scope repo --duration long --durability durable \
  --protocol-version 2 \
  --surface-capabilities '{"durable_agent_jobs":true,"native_subagents":true}'
```

Codex sends `route_feedback` after every enforced native decision. Identical
outcome retries are idempotent; conflicting outcomes fail. `route_reconcile`
releases one session's omitted active decisions after resume, while TTL expiry
recovers callers that never return. `route_status` reports the live mode, policy
version, capacity, TTL, reservation states, and terminal decision-to-feedback
join rate. Session IDs prevent accidental
cross-session updates but are not an authentication boundary against other
processes running as the same macOS user.

When quota routing is enabled, the returned provider/model pair already includes
the supervisor's health decision. Clients must execute that pair when
`enforced=true`; they must not independently reinterpret CodexBar percentages.
`reasons` records any pressure-driven swap. Explicit targets are preserved, and
stale or absent telemetry for the primary leaves the static table unchanged. `route_status`
provides provider health and `quota_alerts` for operator visibility.

When `dynamic_concurrency_enabled=true`, durable-job clients continue to submit
normally; the supervisor alone applies `effective_provider_slots`. A zero slot
count means queued jobs are waiting for cooldown recovery, not rejected. Native
worker capacity remains `fixed_advisory` until its reported feedback join gate
is met and a later rollout explicitly enables enforcement.

When quota routing is disabled, `provider_health` and `quota_alerts` are empty;
the supervisor also preserves legacy provider failure classification.

`job_read(wait_seconds=N)` waits inside the supervisor and wakes on output,
liveness, or terminal state; it does not spin up repeated client connections.
Terminal jobs with an owner produce an at-least-once `job_inbox` delivery that
survives caller and app restarts and remains until exact-owner acknowledgement.
The caller must inspect the retained result before acknowledgement. MCP cannot
proactively inject a result into a suspended model turn, so clients check their
owner inbox on resume or use a host/app notification layer as an external wakeup.

Native Codex, Claude, and Kimi jobs expose provider-neutral semantic events through
`event_cursor`. Native Claude uses its structured stream, so clients can show
reasoning, provider waits, concurrent tool activity, incremental answer text,
usage, and terminal warnings without parsing the raw log. A failed, cancelled,
or interrupted Claude or Kimi run retains all top-level assistant-visible text emitted
before termination in `partial_response`. Treat that field as an ordered work
artifact, not necessarily a polished final answer. Native Claude and Kimi raw
stream JSON is deliberately not returned as `output`/`stdout`; preserve and
advance the event cursor. Kimi emits message-level records rather than token
deltas and keeps stderr-backed output-byte liveness for long tool calls. CAO
compatibility jobs retain output-byte observation until their transports expose
equivalent structured events.

The Kimi semantic kill switch is part of the persisted job specification. A
stable idempotency key retried after that switch changes fails closed as a
different specification instead of returning a job with another output/privacy
contract.

## CAO Compatibility Backend

The supervisor preserves its existing lifecycle contract while delegating
provider execution to CAO. This is an opt-in migration path; native execution
remains the default:

```bash
export AGENT_JOB_EXECUTION_BACKEND=cao
export AGENT_JOB_CAO_URL=http://127.0.0.1:9889
```

`cao_job_bridge.py` maps provider jobs to CAO sessions, forwards the requested
model and workspace, verifies CAO's actual working directory and persisted
read-only tool policy, emits status transitions, retries bounded transport
interruptions, returns the retained final result, and attempts synchronous CAO
session cleanup on every exit. Signal handlers interrupt blocking HTTP calls so
cleanup starts within the supervisor's termination grace. Provider credentials are not
forwarded to the bridge; CAO launches each native provider with its own local
authentication.

Optional settings are `AGENT_JOB_CAO_TOKEN` and
`AGENT_JOB_CAO_LAUNCH_TIMEOUT`; launch requests are capped at eight seconds so a
cancelled bridge retains time for cleanup before forced termination.
The supervisor's existing hard deadline, soft-stall state, owner binding,
idempotency, durable logs, cursor reads, cancellation, and retention continue to
apply outside CAO. The selected backend is persisted per job, so queued work
does not change transport when configuration changes.

CAO read-only execution is enabled only for Claude and Kimi, whose adapters
enforce native tool denial. Read-only Codex fails before launch because this CAO
fork currently launches Codex without an enforceable sandbox. A positive
`max_turns` also fails closed because CAO has no equivalent limit; the normal
unlimited value remains supported and bounded by the wall-clock deadline.

During the pilot, CAO emits status transitions and a retained final result, not
the provider's incremental token stream. A long quiet processing state can
therefore become `possibly_stalled` even while CAO is alive. The hard deadline
still bounds it. Keep the backend opt-in until the observation window confirms
provider status quality and lease cleanup under real workloads.

Compatibility sessions carry the supervisor hard deadline as a CAO metadata
lease plus `AGENT_JOB_DEPTH`, provider, and job identity in the provider
environment. Same-session child CAO terminals inherit and persist the same
validated lease while the CAO process remains live.
CAO reaps only DB-tracked terminals in expired, dedicated `cao-agent-job-*`
sessions when every tracked terminal carries a matching lease. It deletes those
terminals individually, so an untracked operator-created tmux window is never
removed by the compatibility reaper. Missing, mixed, malformed, or live leases
fail closed.

Compatibility identity survives a CAO server restart through the terminal
metadata database. A new window joining the same session and direct Kimi ACP
startup recover the lease only when every tracked terminal has one matching,
live identity. Mixed, incomplete, expired, or malformed persisted state fails
closed. A fresh unrelated operator session deliberately does not inherit the
lease; normal compatibility children stay in the owned CAO session.

Roll out CAO by provider and exact owner namespace instead of switching every
job at once:

```bash
export AGENT_JOB_EXECUTION_BACKEND=native
export AGENT_JOB_CAO_CANARY_PROVIDERS=claude
export AGENT_JOB_CAO_CANARY_OWNER_PREFIXES=cao-canary:FULL_CAO_COMMIT:
```

`AGENT_JOB_CAO_PROVIDERS` promotes named providers independently of the owner.
`tools/agent_job_migration_gate.py` requires fresh deterministic and live-provider CAO
acceptance reports from the same source commit and model, then at least five
completed canary jobs whose first-to-last completion span is 24 hours, no
interruptions, and a failure rate no greater than ten percent. The evaluator
derives the exact `cao-canary:FULL_CAO_COMMIT:` owner namespace itself and
filters database evidence to the requested model. Threshold arguments can only
tighten those baselines, and the report records every parameter. A passing
gate also verifies the installed LaunchAgent still has native as its default and
contains the exact provider, owner namespace, and CAO URL used for the canary.
Acceptance artifact paths, hashes, source commits, models, and timestamps are
recorded in the verdict. A passing report authorizes a provider-scoped
promotion; it never mutates service configuration itself. Native execution
remains the default until that evidence exists.

To roll back, stop submitting work, let running CAO jobs drain, remove
`AGENT_JOB_EXECUTION_BACKEND=cao`, and restart the supervisor. New jobs return
to native execution; already queued jobs retain their recorded backend.

## Multi-Machine Boundary

The current Unix socket, SQLite database, processes, credentials, workdirs, and
logs are local to one Mac. Run one supervisor per execution host. GitHub remains
the source of truth between hosts. A future cross-machine control plane should
route a job to a host that owns the relevant checkout rather than expose this
user-only Unix socket over the network.
