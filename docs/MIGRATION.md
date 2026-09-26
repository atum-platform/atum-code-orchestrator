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
managed section. It adds the `codex-worker` role and a three-thread Codex native
machine ceiling only when no ceiling is already configured. A legacy
`spark-worker` entry is removed during upgrade only when its description,
configuration path shape, and keys match the installer-owned role. Relocated
installer-owned entries are recognized; entries with custom keys or values are
preserved.

The shared native reservation setting is `AGENT_JOB_NATIVE_RESERVATIONS`. The
legacy `AGENT_JOB_CODEX_NATIVE_RESERVATIONS` name remains a fallback for one
compatibility window; set only the new name on upgraded installations.

Reinstalling preserves known routing, quota, concurrency, backend, Codex and
OpenCode binary, and profile-environment overrides from the existing ACO
LaunchAgent.
The Claude binary is the exception: it is resolved at each launch unless
`AGENT_JOB_CLAUDE_BIN` is set on the install command (see
[Retiring a Claude Launcher Pin](#retiring-a-claude-launcher-pin)).
An explicit environment value on the install command still wins. Policy-owned
values such as approved workspace roots are recomputed from the current release
instead of retaining a stale deployment value. Launchd process transitions are
observed for up to 30 seconds before installation reports failure.

Before replacing an existing supervisor, let all `running` and `launching` jobs
finish. The installer refuses to proceed while active jobs exist. Quit Codex
Desktop, Claude Desktop, and OpenCode before rewriting their configuration so an app
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

## Retiring a Claude Launcher Pin

Earlier installs pinned `AGENT_JOB_CLAUDE_BIN`, sometimes to a machine-local
script that also loaded Claude credentials, such as an untracked
`claude-review-runtime` in the checkout or a `~/.local/bin/claude` shim. The
first reinstall on this release drops the pin. When the pinned launcher is a
script, the installer refuses until you name the credential file that script
read, so the daemon injects the same keys itself:

```bash
.venv/bin/python tools/agent_job_client.py list --status running
AGENT_JOB_PROFILE_ENV=/path/to/credentials.env \
  .venv/bin/python tools/install_agent_job_supervisor.py install
```

The value persists across later reinstalls. Only the supervisor is reinstalled;
client bindings are unchanged, so Claude Desktop can stay open. Submit one Claude
review, confirm that it completes and that `read` reports a `binary_path` in the
newest bundled release, and only then delete the old wrapper file. To roll back,
reinstall with `AGENT_JOB_CLAUDE_BIN` set to the previous launcher.

## Enabling the OpenCode Provider

OpenCode replaces Kimi as the default review target. On each Mac:

1. Install the public CLI; ACO accepts only its 1.x contract:

   ```bash
   brew install anomalyco/tap/opencode
   ```

2. Materialize the OpenCode Go API key from the secret manager into an
   owner-only env file. The value never appears on a command line:

   ```bash
   ( umask 077; { printf 'OPENCODE_API_KEY='; infisical secrets get SECRET_NAME \
       --projectId PROJECT_ID --env ENVIRONMENT --plain --silent; } \
       > ~/.local/state/agent-job-supervisor/opencode.env )
   ```

3. With no jobs running, reinstall only the supervisor and append that file to
   the credential path list, keeping any existing entry:

   ```bash
   .venv/bin/python tools/agent_job_client.py list --status running
   AGENT_JOB_PROFILE_ENV="EXISTING_PROFILE_ENV:$HOME/.local/state/agent-job-supervisor/opencode.env" \
     .venv/bin/python tools/install_agent_job_supervisor.py install
   ```

4. Submit one read-only review with `--provider opencode` and confirm it
   completes with a `usage` event and an answer.

The default Muse Spark Contributor model works only after the OpenCode
workspace's Privacy settings allow paid endpoints that train on request data.
Until someone opts in, reinstall with
`AGENT_JOB_OPENCODE_DEFAULT_MODEL=opencode-go/kimi-k3`; afterwards reinstall
without it.

To rotate the key, rerun step 2; the supervisor reads the file at every launch.
Guidance defaults in coding clients change only when
`tools/install_agent_job_clients.py --apply` next runs with the desktop apps
closed; until then `route_decide` remains authoritative.

To roll back, reinstall the previous release.

## Removing the Kimi Provider

This release deletes the Kimi launch code that the OpenCode release left
dormant: the CLI adapters, agent definitions, model aliases, event decoder,
quota rules, and the `kimi` CAO mapping. Upgrade with `git pull --ff-only` in
the installed checkout and reinstall the supervisor while no jobs are running:

```bash
.venv/bin/python tools/agent_job_client.py list --status running
.venv/bin/python tools/install_agent_job_supervisor.py install
```

- The reinstall drops `AGENT_JOB_KIMI_BIN`, `AGENT_JOB_KIMI_CONCURRENCY`,
  `AGENT_JOB_KIMI_DEFAULT_MODEL`, and `~/.kimi-code/bin` from the LaunchAgent.
- Retained Kimi job rows stay readable. A Kimi job still queued at upgrade
  fails with `launch_error` instead of waiting for its queue deadline.
- Kimi logins are left alone. Delete `~/.kimi` and `~/.kimi-code` by hand if
  Kimi Code is no longer used; approved checks keep denying reads of `~/.kimi`
  while it exists.
- Kimi K3 stays available as `opencode-go/kimi-k3` through OpenCode Go.

To roll back, reinstall the previous release; its Kimi lane still needs a
working Kimi Code subscription.

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
`~/.local/share/atum-agent-jobs`, with Codex, Claude, and OpenCode bindings. The Mac
mini Hermes cluster runs separately from `~/.local/share/hermes-agent-jobs`, uses
`com.hermes.agent-job-supervisor`, and stores state under
`~/.local/state/hermes-agent-job-supervisor`. ACO deployment and rollback must
not modify those Hermes paths.
