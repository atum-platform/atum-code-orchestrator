# Migration and Rollback

## Safety Boundary

Install from `~/.local/share/atum-agent-jobs` on each Mac. Do not move an
installed checkout: client configs and launchd intentionally store absolute
paths. Runtime state remains under `~/.local/state/agent-job-supervisor` and is
reused across upgrades.

ACO bootstrap never reads or writes `~/.hermes/profiles`. Global guidance already
present on a machine is adopted verbatim to preserve local and temporary routing
policy; a fresh machine receives the repository default, so machine-specific
overrides may intentionally differ.

The former `--with-hermes` bootstrap option is retired and intentionally has no
replacement in ACO. Manage Hermes profiles through the independent Hermes
deployment.

All coding surfaces receive a separate installer-owned routing block. This block
may be updated without replacing customized provider guidance. The installer
also migrates its exact previous Claude default while preserving any customized
managed section. It adds the `spark-worker` role and a three-thread Codex native
machine ceiling only when no ceiling is already configured.

The shared native reservation setting is `AGENT_JOB_NATIVE_RESERVATIONS`. The
legacy `AGENT_JOB_CODEX_NATIVE_RESERVATIONS` name remains a fallback for one
compatibility window; set only the new name on upgraded installations.

Before replacing an existing supervisor, let all `running` and `launching` jobs
finish. The installer refuses to proceed while active jobs exist. Quit Codex
Desktop, Claude Desktop, and Kimi before rewriting their configuration so an app
cannot race the atomic update. Existing files receive timestamped adjacent
backups; symlinked config files fail closed.

```bash
.venv/bin/python tools/agent_job_client.py list --status running
.venv/bin/python tools/install_agent_job_clients.py
```

The first command must show no active work. The second command is a dry run.

## Apply

```bash
python3 bootstrap.py
```

The client installer is transactional across its targets. The supervisor verifies
that launchd's reported PID and program path match the new process before
reporting success.

Restart client applications after installation. Verify:

```bash
.venv/bin/python tools/agent_job_client.py ping
.venv/bin/python tools/install_agent_job_clients.py --check
.venv/bin/python tools/install_agent_job_supervisor.py status
```

## Rollback

Stop submitting jobs and let active jobs drain. Restore the timestamped
`*.bak.agent-jobs-*` coding-client files, then reinstall the prior supervisor
from its checkout. The SQLite database and retained job results are not deleted
by either installation. Hermes profile rollback belongs to the Hermes runbook.

For routing-only rollback, reinstall the supervisor with
`AGENT_JOB_ROUTING_MODE=shadow` (or remove that variable) after active durable
jobs drain. Existing native reservations expire automatically; the additive
SQLite columns remain backward compatible and require no down migration.

Quota routing has a narrower rollback: reinstall the service without
`AGENT_JOB_QUOTA_ROUTING=1`. Provider health rows and rate-limit events are
additive evidence and may remain in SQLite; default route selection immediately
returns to the static policy. The CodexBar history files are read-only inputs and
are never modified by this service.

Keep the previous ACO checkout until all coding clients have completed a smoke
run through the standalone service. This does not govern the independent Hermes
checkout or its rollback lifecycle.

## Current Deployment

The Mac mini and MacBook each run an ACO supervisor from
`~/.local/share/atum-agent-jobs`, with Codex, Claude, and Kimi bindings. The Mac
mini Hermes cluster runs separately from `~/.local/share/hermes-agent-jobs`, uses
`com.hermes.agent-job-supervisor`, and stores state under
`~/.local/state/hermes-agent-job-supervisor`. ACO deployment and rollback must
not modify those Hermes paths.
