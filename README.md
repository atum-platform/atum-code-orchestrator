# Atum Agent Jobs

A local, durable supervisor for cross-agent work among Codex, Claude, and Kimi.
It lets any supported coding surface submit work to another provider, observe
incremental progress, recover partial responses, and receive durable completion
notices without keeping one MCP request open.

The versioned routing protocol supports shadow mode everywhere, a Codex canary,
and a multi-surface canary. Focused session work can receive an atomic, expiring
same-family native-worker reservation on Codex, Claude Code, or Kimi Code when
the client declares native support. Complementary-domain execution and every
code review route cross-family; the original caller still assembles, verifies,
and reports the outcome.

An optional local quota broker reads CodexBar history without credentials,
normalizes provider rate-limit failures, and rebalances default specialist routes
when fresh evidence shows pressure. Explicit provider requests are never changed.

The supervisor is coding-agent infrastructure. The Hermes agent cluster is a
separate deployment with its own checkout, service, state, and profile
installer. Hermes may use the same wire protocol, but ACO never installs or
repoints Hermes profiles.

## Install

Clone to the stable path on each Mac, then bootstrap locally:

```bash
git clone https://github.com/anka-ventures-labs/atum-code-orchestrator.git \
  ~/.local/share/atum-agent-jobs
cd ~/.local/share/atum-agent-jobs
python3 bootstrap.py
```

Bootstrap requires Python 3.10 or newer. When Apple `python3` is older, it
automatically restarts with Homebrew Python from a standard install path.

Restart Codex Desktop, Claude Desktop, and Kimi after first installation so they
reload MCP settings.
Claude Code uses the installed skill and guarded CLI rather than a nested MCP
process.

Each machine runs its own supervisor and keeps its own SQLite queue under
`~/.local/state/agent-job-supervisor`. GitHub distributes code and configuration
logic; runtime databases, logs, sockets, tokens, and provider credentials never
sync between machines.

## Supported Surfaces

- Codex: `agent-jobs` MCP plus global `$agent-jobs` guidance and skill.
- Claude Code: global skill and guarded command-line binding.
- Claude Desktop: `agent-jobs` MCP registration.
- Kimi Code: MCP registration, global guidance, and shared skill.
- Hermes: protocol-compatible only; managed by its independent cluster runtime.

Provider execution uses the locally authenticated `codex`, `claude`, and `kimi`
CLIs, so usage is charged to the account or subscription configured in each CLI.

## Operations

```bash
.venv/bin/python tools/agent_job_client.py ping
.venv/bin/python tools/install_agent_job_clients.py --check
.venv/bin/python tools/install_agent_job_supervisor.py status
.venv/bin/python tools/agent_job_client.py route-status
.venv/bin/python -m unittest discover -s tools/tests -v
```

See [supervisor internals](docs/AGENT_JOB_SUPERVISOR.md) and
[client integration](docs/CLIENT_INTEGRATION.md) for protocol and recovery
details. ACP/CAO remains an optional compatibility backend; native provider CLIs
are the production default.

For an existing installation, follow the [migration and rollback runbook](docs/MIGRATION.md)
instead of restarting a supervisor with active jobs.

The extracted supervisor code retains the upstream Hermes Agent MIT license.
