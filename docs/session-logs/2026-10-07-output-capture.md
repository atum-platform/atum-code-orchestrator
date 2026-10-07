# Review output capture repair — 2026-10-07

## Scope and evidence

Moon's owner authorized troubleshooting and repairing the shared review tooling
after a failed OpenCode review and an exit-zero Claude fallback with no retained
answer. This is an ACO outcome, not Moon deployment or production activation.
Base: `db7e281ec1f28b1607ba8846402798cf6eca669b`.

The OpenCode job `f869c268-a6e9-4819-81e5-8b132eecf799` tried a
machine-specific workspace path from Moon's repository instructions and was
correctly denied outside its staged snapshot. Its retained response was progress,
not a verdict. Claude job `e00abcfd-6fcb-4a76-8dc5-cfde823884fd` exited zero;
private stdout retained only initialization/status/thinking metadata and message
starts, not an assistant/result answer. Neither was accepted as a review.
Private `.log.stdout`, not the deliberately absent combined `.log`, is the raw
artifact for these native semantic providers. Raw sizes were below default caps.

The exact historical capture-loss cause is not proven. Host free space dropped
to about 118 MiB, temporary SQLite initialization failed with disk I/O error,
and an initial source patch could not be written. No cleanup of unrelated data
was performed. Free space subsequently recovered to about 2.3 GiB.

## Plan — parallel MECE workstreams

1. Coordinator owns subprocess reader lifecycle, sanitized failure classification,
   focused regression tests, and snapshot-local reviewer guidance.
2. Native read-only decoder audit owns supported Claude stream-shape inspection;
   it confirmed existing assistant/result recovery works. No decoder runtime
   change is needed. An incomplete-start characterization must not invent text.
3. Native read-only release audit owns supervisor-only update/refusal/rollback
   inspection. No source edits, provider runs, or service mutation in that scope.

Assemble once, verify focused/full suites, then request one primary cross-family
review (at most one targeted finding follow-up). Publish one repair PR; require
exact-head hosted CI and retain the verdict before merging/installing.

## Behavior and verification

Previously `_run_job` discarded exceptions from its stdout/stderr reader tasks;
an exit-zero process could be marked completed after losing output. The new
regression failed on that exact old behavior after the initial disk failure.

Each reader now signals a per-job capture-failure future and continues discarding
its exact child's pipe until termination/EOF, avoiding full-pipe deadlock. The
supervisor checks that signal while running and again after both readers settle.
It marks `failed/output_capture`, not success, even after exit zero. Explicit
cancellation/deadline outcomes retain priority. Diagnostics contain only stream,
exception class and optional errno, never exception prose/provider text/paths.
Handled semantic-normalization failure still preserves its existing raw recovery
contract; no permission, output budget, database schema, routing or credentials
change. Successful process completion is still not, by itself, a usable verdict;
callers must inspect the actual retained answer.

OpenCode's isolated reviewer prompt explicitly keeps all inspection in the current
snapshot using repository-relative paths while preserving relevant project
requirements. External-directory/tool denials are unchanged.

Five focused tests passed: capture failure, stdout/stderr backpressure with prompt
child termination, post-EOF failure retaining prior visible text, cancellation
priority, and existing normalization fallback. Full suite, independent review,
hosted CI, installation and real repaired-service review are pending.

The first full suite ran345 tests with two failures: installer fixture checks
require this checkout's `.venv/bin/python` path, absent in the fresh worktree.
No source fix was made for that environment error; the existing development
environment was linked (ignored `.venv`) and the full suite rerun. OpenCode
command isolation plus incomplete-Claude-start characterization pass18/18;
Python compilation and diff whitespace checks pass. Corrected full suite passes
346/346 in82.537s, including the new incomplete-start case. `.gitignore` accepts
either a directory or link for the local `.venv` path; no environment contents
are tracked.

## Operating boundaries and continuation

Installed source stays at `/Users/NM/.local/share/atum-agent-jobs`; runtime SQLite
state and client configs must not move or be rewritten. Do not run bootstrap or
the client installer for this supervisor-only change. Update the clean installed
checkout to an accepted exact release, retaining the previous commit, then use
its existing environment's `tools/install_agent_job_supervisor.py install`.

Freeze submissions and verify no queued, launching or running jobs immediately
before replacement. Installer currently checks only launching/running after a
successful ping; a failed initial ping does not prove idleness, and it has no
submission maintenance lock. This pre-existing installation hazard is not fixed
by this capture patch. If reachability/drain/serialization cannot be established,
hold installation instead of interrupting other sessions. Check ping and launchd
PID/program identity after installation. Rollback: the same serialized drain,
restore the prior installed source commit, reinstall only the supervisor, retain
state/config. Never install from the temporary repair worktree.

After repaired-service evidence, resume Moon PR #380's held source review with
snapshot-local instructions. No Moon migration, enrollment, customer send, model
spend rehearsal or production action is authorized by this repair.

## Routing receipts

- Coordinator: `8b24ac86-ffeb-4225-ad69-e099b06a8759` (direct).
- Decoder audit: `4dfc8d48-14ae-42ad-aa0c-16e439862edf`, completed/released.
- Release audit: `5ea26484-7e46-45d5-8202-c2a6fae73fbe`, completed/released.
- Assembly review route: `7dcc5bac-d42a-4129-a2cc-ec668de09deb` (OpenCode).
- Assembly job: `7c8690c4-67e9-487c-a1fc-6d8a4a6bc8d5`, exact owner
  `codex:moon-review-output-repair-20261007:output-capture-assembly`;
  final read cursor0 / event_cursor15394, complete visible result4347 bytes.
  Full verdict read: SHIP SOURCE (hold install/activation), no introduced blocking
  finding. Exact-owner delivery acknowledged; review route feedback completed.
- Coordinator direct route feedback completed. All three routed scopes closed.

Primary review confirms exit-zero loss, pipe backpressure, privacy, cancellation
priority and permission preservation. Its small hygiene/test suggestions are
non-blocking and deferred: retain conservative failure on an unexpectedly
cancelled individual reader, first-failure diagnostics and simple future lifetime;
timeout priority is covered by unchanged loop ordering and existing full-suite
timeout cases, not a new capture/deadline race test. No second review needed.
The reviewer snapshot predates the full346 pass and documentation-only update;
runtime and test sources are identical to the reviewed checkpoint.
Exact-head hosted CI, installation and resumed Moon review remain pending.
