# Agent Job Operations

## Guarded Review CLI

The MCP tools and this CLI call the same safety core.

```bash
python3 scripts/review.py submit \
  --provider claude \
  --model opus \
  --workdir /absolute/project \
  --instructions "Review this architecture using the planning rubric." \
  --expected-output "Severity-ordered findings and a verdict." \
  --timeout-seconds 1800 \
  --idempotency-key session-checkpoint-claude
```

For code review, add `--context-git-diff --context-git-base <base-ref>`.

```bash
python3 scripts/review.py read JOB_ID --cursor 0 --event-cursor 0 --wait-seconds 30
python3 scripts/review.py list --status running
python3 scripts/review.py cancel JOB_ID
python3 scripts/review.py inbox --owner codex:task-name
python3 scripts/review.py inbox --owner codex:task-name --ack-delivery-id DELIVERY_ID
```

Preserve both returned cursors and pass them to the next read. `cursor` advances
the human-readable combined log; `event_cursor` advances normalized semantic
events. A job survives the calling session, MCP process, or app. Recover unknown
IDs with `list` and filter by the owner prefix used at submission.

Terminal jobs with a non-empty owner create an at-least-once inbox delivery.
Inbox reads are non-destructive and exact-owner scoped. Inspect the retained job
result before acknowledging the returned delivery ID; do not assume it is the
same identifier as the job. A server-side `read --wait-seconds`
holds one bounded socket request and wakes on output, liveness, or terminal state.

Treat `job.lifecycle_status` as authoritative and `job.activity` as the current
semantic observation. `tool_running:<name>` means a quiet provider still has an
open top-level tool; `open_tool_count` reports concurrent tools without changing
the name contract. `idle_unknown` means the process
is alive but has produced no semantic progress past the soft threshold. Terminal
reads include `partial_response` plus `partial_result_state` (`complete`,
`partial`, `truncated`, `none`, or `unavailable`), so inspect retained output
before retrying a failed or cancelled run. For native Claude and Kimi, the partial
response is top-level assistant-visible text emitted in order and may stop
mid-answer; it deliberately excludes subagent text and the duplicate terminal
result. Kimi emits complete message chunks rather than token deltas, so text
buffered inside an interrupted provider step may not have reached the stream.
Their raw stream JSON is not returned to ordinary callers, so retain and
advance `event_cursor`. `unavailable` means that provider/backend does not expose a semantic
response artifact; use the retained raw output instead.
`journal_truncated=true` means normalized events reached their independent byte
budget even though raw output capture may have continued.

Detailed semantic activity is available for native Codex, Claude, and Kimi jobs.
Claude provider waits and concurrent open tools are explicit. A provider wait
that exceeds the soft threshold becomes `idle_unknown`; it never hides a hung
request until the run deadline. Kimi still uses output-byte liveness because
its JSON stream has no tool-start boundary; public stderr tool progress provides
that transport signal but is not promoted into semantic event content. CAO
compatibility jobs also use output-byte liveness, so their
`waiting_on_provider` state does not carry the same structured evidence.

Statuses:

- `queued`: waiting for provider capacity.
- `launching`: atomically claimed; process identity is being recorded.
- `running`: active persisted lifecycle state.
- `possibly_stalled`: compatibility alias for active semantic silence; not
  terminal. Use `activity` and `seconds_without_progress` for diagnosis.
- `completed`, `failed`, `cancelled`, `interrupted`: terminal.

## Explicit Implementation CLI

```bash
python3 scripts/delegate.py \
  --provider claude \
  --model opus \
  --mode implement \
  --workdir /absolute/project \
  --prompt "Implement only the scoped change. Run the unit check. Do not commit or push." \
  --check 'unit=npm test -- --runInBand'
```

The script prints `AGENT_JOB_ID` before polling. If the shell exits, recover the
job through the guarded review CLI's `list`/`read` operations or the low-level
`agent_job_client.py`.

Repeat `--check 'NAME=COMMAND'` for focused caller-approved verification. The
delegated model chooses only a name; it cannot alter the argv. Do not approve
Git, installs, deployments, dev servers, external side effects, or commands that
require secrets. The originating agent runs final verification itself.

## CAO Canary Operations

Native execution remains the default. An operator can route only jobs whose
provider and exact owner prefix match the canary configuration:

```bash
export AGENT_JOB_EXECUTION_BACKEND=native
export AGENT_JOB_CAO_URL=http://127.0.0.1:9889
export AGENT_JOB_CAO_CANARY_PROVIDERS=claude
export AGENT_JOB_CAO_CANARY_OWNER_PREFIXES=cao-canary:FULL_CAO_COMMIT:
python3 tools/install_agent_job_supervisor.py install
```

Submit canary work with a stable owner such as
`cao-canary:FULL_CAO_COMMIT:claude:checkpoint`.
Ordinary owners continue through native execution. Do not promote on a smoke
test alone: run `tools/agent_job_migration_gate.py` with matching mock and live
CAO gate reports after at least five completed jobs spanning 24 hours.
The command returns `promote` or `hold` as structured JSON and does not change
the service. It verifies the installed LaunchAgent contains the exact full-commit
owner prefix, provider, CAO URL, and native default; a shell-only export without
reinstalling the service cannot pass. On rollback, remove the canary/provider
settings, reinstall the service, and allow existing CAO jobs to drain; their
recorded backend remains unchanged.
