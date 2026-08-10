# Migration and Rollback

## Safety Boundary

Install from `~/.local/share/atum-agent-jobs` on each Mac. Do not move an
installed checkout: client configs and launchd intentionally store absolute
paths. Runtime state remains under `~/.local/state/agent-job-supervisor` and is
reused across upgrades.

`bootstrap.py --with-hermes` refuses to run outside that stable checkout. Global
guidance already present on a machine is adopted verbatim to preserve local and
temporary routing policy; a fresh machine receives the repository default, so
machine-specific overrides may intentionally differ.

Before replacing an existing supervisor, let all `running` and `launching` jobs
finish. The installer refuses to proceed while active jobs exist. Quit Codex
Desktop, Claude Desktop, and Kimi before rewriting their configuration so an app
cannot race the atomic update. Existing files receive timestamped adjacent
backups; symlinked config files fail closed.

```bash
.venv/bin/python tools/agent_job_client.py list --status running
.venv/bin/python tools/install_agent_job_clients.py
.venv/bin/python tools/install_hermes_profiles.py
```

The first command must show no active work. The other two are dry runs.

## Apply

```bash
python3 bootstrap.py
.venv/bin/python tools/install_hermes_profiles.py --apply  # only on a Hermes host
```

The client installer is transactional across its eight targets. Hermes profile
migration is transactional across every applicable profile and preserves each
profile's enabled and timeout settings. The supervisor verifies that launchd's
reported PID and program path match the new process before reporting success.

Restart client applications after installation. Verify:

```bash
.venv/bin/python tools/agent_job_client.py ping
.venv/bin/python tools/install_agent_job_clients.py --check
.venv/bin/python tools/install_agent_job_supervisor.py status
```

## Rollback

Stop submitting jobs and let active jobs drain. Restore the timestamped
`*.bak.agent-jobs-*` client files and profile backups, then reinstall the prior
supervisor from its checkout. The SQLite database and retained job results are
not deleted by either installation.

Keep the previous checkout until all coding clients and optional Hermes profiles
have completed a smoke run through the standalone service. After that, the old
Hermes copy is historical source only and can be removed once its Git state is
confirmed pushed.

## Current Deployment

As of 2026-08-10, the Mac mini and MacBook each run one local supervisor from
`~/.local/share/atum-agent-jobs`. Codex, Claude, and Kimi bindings are installed
on both. Hermes profiles are consumers only: twelve Mac mini profiles were
migrated, while the MacBook had no applicable profile registration. End-to-end
Codex smoke jobs completed on both hosts. The legacy checkout is retained only
for short-term rollback and is not referenced by live configuration.
