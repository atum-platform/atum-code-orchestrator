# Supervisor restart availability

## Problem

On 2026-09-04 the supervisor stopped at 14:49:54 while a Codex implement job
was running. The socket disappeared, client and MCP calls failed with
`[Errno 2] No such file or directory`, and nothing could be submitted for about
five minutes.

What the recorded evidence actually shows:

- Job `1b580fc5` (implement, pid 66639) was marked `interrupted` /
  `supervisor_shutdown` at 14:49:54.977, in both `jobs.sqlite3` and its event
  journal. Only the `CancelledError` path in `_run_job` writes that
  `failure_kind`, and that path is reached only from the `SIGTERM`/`SIGINT`
  handler. The daemon ran its graceful shutdown; it did not crash.
- `supervisor.stderr.log` has an mtime of 2026-08-24, so no traceback was
  written on 2026-09-04. The `ConnectionResetError` in `client_connected_cb`
  recorded in that file predates the incident, and could not have been fatal in
  any case: an error raised in an asyncio connection callback is reported to
  the loop's exception handler, not propagated into the process.
- Job `ff197c35` was not lost. It was accepted at 14:49:54.963, 14 ms before
  the shutdown record, survived as a durable queued row, started at 14:55:11
  under the next daemon, and completed with exit code 0 at 15:16:50 after
  writing a 105-line log. Durability across a caller's session ending held.
- Startup was never slow. Binding the socket over the real 6.7 MB, 512-job
  database takes 0.454 s measured. The database is small, and nothing scans the
  log directory at startup.

The outage was therefore not a crash, a lost job, or slow recovery. A signalled
supervisor exits `0` (measured), and the LaunchAgent carried
`KeepAlive = {"SuccessfulExit": false}`, which tells launchd that a clean exit
is intentional and the service should stay down. One signal took the daemon out
until somebody restarted it by hand, and that is the five-minute window. The
socket was absent because no daemon was running, not because a daemon was slow
to bind.

What sent the original `SIGTERM` is not recoverable. The unified log archive
does cover 2026-09-04 14:49 and holds 11,960 events for that minute, but
contains no record for pids 66639, 83605 or 88568, no `launchd` entries for the
label, no jetsam or memory-pressure event, and no `xpcproxy` spawn: launchd
does not persist LaunchAgent exit records at a level this archive retains. The
installer is an unlikely source, since it refuses to replace the supervisor
while a job is active, and the plist was last written on 2026-08-30.

Two further defects were latent rather than implicated:

- Shutdown terminated each provider child, waiting up to ten seconds per job,
  before recording that job's terminal row, while launchd's default
  `ExitTimeOut` is five seconds. A shutdown with a slow child was guaranteed to
  be SIGKILLed part-way through, losing the remaining rows.
- Restart recovery reaped orphaned process groups before binding the socket, at
  two `ps` probes plus a one-second grace period per job, serially. A restart
  with jobs in flight made the supervisor unreachable for as long as recovery
  took.

## Change

- `KeepAlive` is now unconditionally true, so a signalled or killed supervisor
  always returns. `ExitTimeOut` is 30 seconds, above the daemon's own
  `AGENT_JOB_SHUTDOWN_GRACE_SECONDS` (15), so graceful shutdown is no longer
  cut short. `ThrottleInterval` is 10 seconds.
- Shutdown records a terminal row for every in-flight job in a single commit
  before it does anything slow, so the rows survive even when the platform
  kills the process before the per-job handlers finish.
- The whole job-task shutdown is bounded by `AGENT_JOB_SHUTDOWN_GRACE_SECONDS`
  and logs `supervisor_shutdown_timeout` if it is hit.
- Restart recovery is split. The caller-visible half, marking previously
  running jobs `interrupted`, stays before the bind and is pure SQL. Reaping
  their orphaned process groups moved behind the bound socket and now runs in
  parallel, so startup latency no longer scales with the number of interrupted
  jobs.
- The daemon emits one JSON lifecycle line per start and stop on stderr. It
  previously recorded nothing, which is why this incident had to be
  reconstructed from job rows and mtimes. A lock conflict now logs
  `supervisor_already_running` instead of exiting silently.
- Response writes to an abandoned caller are guarded on `OSError` rather than
  only `BrokenPipeError`/`ConnectionResetError`, and `close`/`wait_closed` are
  guarded too. This hardens the old `client_connected_cb` path regardless of
  its role here.

## Verification

`.venv/bin/python -m unittest discover -s tools/tests` passes, 304 tests: the
297 that passed before this change, plus seven new ones.

Each new regression test was confirmed to fail against the previous behaviour,
not merely to pass against the new one:

- Reverting the up-front terminal mark makes
  `test_shutdown_records_terminal_state_before_terminating_children` fail with
  `['interrupted'] != ['running']`.
- Restoring pre-bind reaping makes
  `test_restart_binds_socket_before_reaping_orphaned_processes` fail with the
  incident's own symptom, `SupervisorUnavailable: ... [Errno 2] No such file or
  directory`.

Measured directly against the production state directory: `SIGTERM` yields
exit code 0, and bind latency over the real database is 0.454 s.

## Follow-up

- The live LaunchAgent still carries the old `KeepAlive` dictionary. Applying
  the new plist needs `install_agent_job_supervisor.py`, which replaces the
  service and so requires an empty queue; it refuses while a job is active.
- A macOS `disk writes` resource report (`Python_2026-09-05-112733`) attributes
  8.59 GB of dirty file-backed writes over 14 hours to the
  `com.atum.agent-job-supervisor` coalition, with 83 of 111 samples inside a
  `PRAGMA` that re-reads the schema and re-maps the WAL index
  (`walTryBeginRead`, `unixShmMap`, `pwrite`), at background QoS on an
  efficiency core. The sampled pid was started as `python -m`, so it is a
  process in the coalition rather than the daemon itself. Unrelated to this
  outage, but worth tracing: the host had 5.6 GB free against a 3 GB low-space
  threshold.
