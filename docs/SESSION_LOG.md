# Session Log

## 2026-08-24 - Cross-surface client parity

- Added Claude Code's official user-scope `~/.claude.json` to the transactional
  client installer so Claude Code receives the same read-only `agent-jobs` MCP
  binding as Codex, Claude Desktop, and Kimi Code. Live `claude mcp list`
  verification proved that this installed Claude version ignores MCP entries in
  `~/.claude/settings.json`; installation therefore requires inactive Claude
  Code sessions and preserves the full user-scope document atomically.
- Made the plan-of-parallels protocol part of the replaceable routing guidance
  block, eliminating cross-machine drift while preserving locally owned
  provider policy.
- Updated installer coverage and client documentation. Installation remains
  native per machine; configuration files containing local credentials are
  never copied between Macs.
- Refreshed PR #22 by merging current `main` at
  `63d9477607fc3f51f0d34c5761f2c8e3610b1cf0`. The merge adopts the hosted
  Ubuntu workflow and portable launch-identity assertion from PR #23; no
  duplicate supervisor or test fix was authored on this branch.
- Verification after the refresh: the launch-identity and recursive-submission
  tests pass (2/2), the supervisor integration class passes (113/113), the full
  repository suite passes (293/293), Python compilation succeeds, and
  `git diff --check` is clean.

## 2026-08-24 - Prevent WAL fsync from starving control requests

- Reproduced a 40-second direct supervisor-socket timeout during a live Kimi K3
  smoke, proving the problem existed below MCP and provider run-time ceilings.
- A process sample found the single asyncio control thread committing and
  checkpointing SQLite WAL frames with `fsync` while socket requests waited.
- Kept WAL but selected `synchronous=NORMAL` and 256-page automatic checkpoints,
  trading only the newest queue updates on sudden power loss for bounded local
  commit latency. Provider processes remain identity-reconciled after restart.
- Verification: 2 focused persistence/control-plane tests pass; the full
  292-test suite passes; `git diff --check` is clean. Post-deployment latency is
  checked on both supervisors. The MacBook sustained 160 route-decision and
  feedback writes with 18.14 ms p95 and 52.03 ms maximum interleaved ping
  latency, versus the reproduced 40-second timeout. Fifty deliberately reset
  clients added zero bytes to supervisor stderr. Both machines run the canonical
  supervisor SHA-256 `79682cdf1db14030262e6f60d5b1c689ef86c23c3066755410a982d52213655a`.

## 2026-08-24 - Preserve modern Kimi model configuration

- A live MacBook smoke reached Kimi Code 0.31.1 but proved the isolated runtime
  lacked its configured `kimi-code/k3` model declaration.
- Modern Kimi jobs now copy the authenticated `config.toml` into the private
  per-job home with mode `0600`, while continuing to replace MCP state and use
  an empty Skills directory. Missing configuration fails before provider launch.
- Verification: 3 focused modern-Kimi tests pass; the full 291-test suite
  passes; `git diff --check` is clean. MacBook smoke job
  `98c47bd3-2475-49cb-8c53-d592758185b6` completed on K3 with the exact retained
  response `ACO KIMI OK`.

## 2026-08-24 - Keep MCP polling below transport ceilings

- Capped MCP `job_read` waits at 10 seconds while preserving the guarded CLI's
  60-second long-poll option, so coding-surface socket ceilings cannot be
  mistaken for Opus or supervisor failure.
- Increased the local Unix client response margin and treated reader disconnects
  as an expected abandoned poll instead of emitting unhandled supervisor errors.
- Durable jobs continue independently of MCP requests; callers recover progress
  and terminal results with the retained job ID, cursors, and owner inbox.
- Verification: 152 focused client, MCP, and supervisor tests pass; the full
  291-test suite passes; `git diff --check` is clean.

## 2026-08-24 - Harden mediated verification after Opus review

- Kept the named-check broker available only to Claude implementation jobs;
  Codex and Kimi submissions with checks now fail closed until their equivalent
  mediated tool paths are proven end to end.
- Added broker signal/parent-loss cleanup plus supervisor-owned, identity-checked
  reaping of recorded check process groups on cancellation, timeout, shutdown,
  and restart cleanup.
- Expanded the inner macOS profile to deny Apple Events, common launchd/script
  escapes, and the local credential stores used by ACO providers and package
  tooling; added broker-side contract validation and a missing Kimi MCP-config
  guard.
- Removed legacy idempotency-hash compatibility for submissions carrying checks,
  so an old row cannot match a different verification contract.
- Documented that caller-approved project checks execute model-influenced
  repository code and that the targeted Seatbelt profile reduces blast radius
  rather than providing a complete default-deny execution boundary.
- Review: Opus 5 held phase 6 on orphaned check processes, unsupported Codex
  mediation, and an unverified Kimi tool path; this checkpoint addresses each
  release blocker before deployment.
- Verification: 12 focused broker, delegation, confinement, and submission tests
  pass; the full 284-test suite passes; Python compilation and `git diff --check`
  are clean.

## 2026-08-24 - Close cross-surface routing and compatibility gaps

- Installed the enforced routing protocol into Codex, Claude Code, and Kimi Code
  guidance while preserving locally owned provider policy sections.
- Made the cooperative native reservation pool explicitly provider-neutral via
  `AGENT_JOB_NATIVE_RESERVATIONS`, retaining the previous Codex-prefixed name as
  a compatibility fallback and proving capacity is shared across surfaces.
- Routed focused same-family Claude work to Sonnet and focused Kimi work to
  high-speed K2.7, while preserving cross-family code review for every caller.
- Restored `max_turns` as an accepted but ignored compatibility input at MCP and
  CLI boundaries; run deadlines remain the sole effective execution ceiling.
- Verification: 69 focused routing, client, and guarded-interface tests pass;
  the full 279-test suite passes; `git diff --check` is clean.
- Review: Opus 5 returned a ship verdict and confirmed the compatibility, model,
  and cross-family-review changes. Its follow-up findings were applied: exact
  stale Claude defaults now migrate without overwriting custom policy, the shared
  skill describes every same-family native lane, cross-surface capacity has a
  positive control, protocol-v1 native routing pins its Codex-only invariant, and
  migration documentation covers the provider-neutral setting. The 50 focused
  tests covering those corrections pass.

## 2026-08-24 - Recover deployed routing changes into source control

- Recovered the tracked delta from the active
  `~/.local/share/atum-agent-jobs` runtime into the canonical repository instead
  of leaving the running protocol as an uncommitted installation artifact.
- Preserved the deployed same-family native-worker routing for Codex, Claude,
  and Kimi, complementary-family durable routing, retired public provider turn
  ceilings, updated client guidance, and MECE assembly-review instructions.
- Reconciled stale tests and payload fixtures with the deployed contracts:
  legacy nonzero `max_turns` input is normalized to unlimited, cross-family
  degradation tests use code review rather than same-family planning, and MCP
  parity no longer submits the removed field.
- Verification: 40 focused routing/supervisor/review-core tests pass; the full
  274-test suite passes; `git diff --check` is clean.
- The source checkpoint is intentionally not deployed yet. Claude tool-surface
  enforcement, supervisor consolidation, filesystem confinement, and mediated
  commands remain separate reviewed checkpoints.

## 2026-08-13 - Use the organization Actions runner

- Moved repository tests from GitHub-hosted macOS to the private organization
  ARM64 Mac Mini runner.
- Added same-repository pull-request admission and cancellation of superseded
  workflow runs.
- Verification: validate the workflow with `actionlint`, run the 268-test local
  suite, and confirm the migration PR reports runner `mac-mini-anka-labs`.

## 2026-08-13 - Separate queue and run timeouts

- Added independent persisted `queue_timeout_seconds` and
  `run_timeout_seconds` budgets. Queue time begins at submission; run time begins
  only after provider launch.
- Set new defaults to 15 minutes queued and 45 minutes running, each bounded to
  30 seconds through two hours. Retained `timeout_seconds` as a deprecated alias
  for the run budget.
- Preserved legacy persisted jobs with their original submit-relative shared
  deadline instead of rewriting live semantics during migration.
- Added proactive queue expiry, explicit deadline/status fields, idempotency
  compatibility with pre-migration jobs, client/CLI parameters, and regressions
  covering independent clocks and legacy behavior.
- Verification: Python compilation and all 268 repository tests pass; `git
  diff --check` is clean.
- Review: the routed Kimi K3 assembly review produced no review output after 18
  minutes and was cancelled after process-level inspection. The enforced
  one-hop route returned `direct`; the local assembly review found no merge
  blocker.

## 2026-08-13 - Preserve explicit provider overrides

- Corrected terminal quota handling so exhaustion blocks only automatic
  selection, fallback, escalation, and native-worker routing.
- Explicit provider/model requests now remain executable operator overrides;
  durable submission and scheduler capacity no longer reject those jobs.
- Kept `exhausted` visible in route status so callers can report the quota risk
  while honoring deliberate model selection.

## 2026-08-13 - Enforce terminal quota exhaustion

- Traced live Opus calls despite Claude's 99% weekly usage. Default calls were
  routing correctly to Kimi, but Codex sessions marked their own reviewer
  preference as `explicit_provider=claude`, which intentionally bypassed the
  pressure balancer; one-hop fallback also allowed pressured providers.
- Added a distinct `exhausted` provider state based on actual utilization at
  98% or above, with recovery below 95%. Projected pressure remains advisory.
- Made exhaustion a hard boundary across default routing, explicit targets,
  one-hop escalation, dynamic concurrency, and direct job submission.
- Cancelled the sole active Opus job after confirming it had no partial result.
- Verification: all 260 repository tests pass under the supported project
  virtual environment, along with Python bytecode compilation and
  `git diff --check`. Kimi K3's assembly review found two pre-ship issues:
  queued jobs were only held under dynamic concurrency, and two temporary
  cooldowns degraded to direct instead of retaining automatic recovery. Both
  were corrected, with additional native-worker and scheduler regressions.
  The corrected focused suite passes 147 tests. Live supervisor restart and
  admission probes remain.

## 2026-08-13 - Require routing before cross-agent submission

- Diagnosed live Codex design jobs that submitted Opus directly despite the
  quota broker routing Claude's 100% projected pressure to Kimi K3. Those jobs
  had no corresponding `route_decisions` rows.
- Expanded the managed Codex protocol from native-worker routing to every
  cross-agent submission. Enforced route decisions now explicitly supersede
  static Opus/Kimi preference text.
- Updated the shared skill and client contract to require route-first durable
  submissions, terminal feedback, and one-hop rerouting after provider failure.
- Kimi K3's primary review held the change for two wording gaps. Restored the
  static fallback path for shadow/outage failures, clarified canary enforcement
  by caller surface, documented shell routing commands, and added an in-place
  managed-block upgrade regression test.
- Verified the corrected route-first contract with 256 repository tests, Python
  bytecode compilation, and `git diff --check`.
- Kimi K3's targeted follow-up verified all four corrections and returned SHIP
  with no remaining actionable defect in the Codex route-first path.

## 2026-08-13 - One-hop routing escalation P5

- Added protocol-v2 escalation intents with a parent decision ID, typed reason,
  bounded evidence, and stable caller/session identity.
- Escalation requires the parent to record `route_feedback=escalated`, excludes
  its provider after quota/cooldown routing, clears further fallbacks, and allows
  at most one child. Native-worker failure degrades to direct primary execution.
- Added atomic parent validation and a unique parent-child index. Identical
  retries return the retained child response; conflicting retries fail closed.
- Reused the guarded review redactor before persisting escalation evidence.
- Exposed escalation fields in MCP, CLI, and shared skill guidance.
- Kimi K3's primary review found the mechanics sound but flagged ambiguity around
  fallback health, route-shape changes, and explicit targets. Clarified the
  intentional fail-open quota semantics and identity contract, bounded evidence
  after redaction, improved MCP discovery text, and added the missing edge tests.
- Verified the completed phase with 254 repository tests, focused escalation
  integration tests, Python bytecode compilation, and `git diff --check`.
- Kimi K3's targeted follow-up returned SHIP. Added its sole non-blocking
  recommendation: a regression test for native-worker escalation degrading to
  direct primary execution.

## 2026-08-13 - Capability-aware mixed-version routing P4

- Added routing protocol v2 while retaining full protocol-v1 acceptance and
  behavior. The CLI defaults to v2; MCP omission remains v1-compatible, and new
  client guidance includes a one-time v1 retry against older supervisors.
- Added server-owned surface capability ceilings. V2 intersects client claims
  with those ceilings and degrades unavailable durable lanes to `direct` rather
  than returning instructions the calling app cannot execute.
- Added exact Codex/Claude/Kimi capability matrices: GPT-5.6 Sol handles Codex
  durable targets, Opus handles Claude review and
  deep thinking domains, K3 handles Kimi review and standard/deep work, and
  Fable remains explicit-only.
- Added opt-in `surface_canary` enforcement for cooperating v2 Codex, Claude,
  and Kimi surfaces while old non-Codex clients remain shadow.
- Exposed supported protocol versions and both matrices in `route_status`; added
  mixed-version, exact-model, false-capability, and degradation regressions.
- Verification passes all 242 unit/integration tests, Python compile checks, and
  diff whitespace validation.
- Kimi K3 returned SHIP and identified two mixed-version ambiguities. Gated every
  `surface_canary` caller, including Codex, to v2; derived exact models from the
  published matrices; automated CLI fallback to v1-only supervisors; and
  documented trusted-local identity plus caller/surface validation in both
  protocol versions.
- Post-review verification passes all 245 unit/integration tests, Python compile
  checks, and diff whitespace validation.
- Kimi K3's targeted follow-up returned SHIP with no blockers. Its remaining
  model-consistency note was resolved by returning the concrete Spark model ID
  for v2 native-worker decisions while preserving `codex_fast` for v1.

## 2026-08-12 - Dynamic durable concurrency P3

- Raised each durable provider's default and hard concurrency ceiling to three.
- Added opt-in quota-aware effective slots: pressure removes one slot, canonical
  rate-limit cooldowns pause new launches at zero, and recovery restores capacity
  without cancelling jobs already running.
- Kept native reservations fixed and advisory. Status reports the 95% feedback
  join prerequisite instead of silently treating today's 50% live rate as ready
  for dynamic enforcement.
- Added startup, scheduler recovery, status, and installer propagation coverage.
- Verification passes all 227 unit/integration tests, Python compile checks, and
  diff whitespace validation.
- Kimi K3 conditionally approved the scheduler design. The three-slot default and
  hard ceiling for every provider is an intentional product decision from the
  concurrency roadmap, not part of the feature flag. Follow-up hardening validates
  the dynamic/quota pairing before replacing launchd, cleanly handles startup
  configuration errors, clarifies hysteretic recovery, and adds the requested
  scheduler-level capacity, running-job, automatic-recovery, bounds, stale-health,
  and native-gate tests.
- Post-review verification passes all 233 unit/integration tests, Python compile
  checks, and diff whitespace validation.
- Kimi K3's targeted follow-up returned SHIP with no blockers. The installer now
  also normalizes every supported boolean spelling during preflight, closing its
  non-blocking parity note before release.

## 2026-08-12 - Local quota broker P2

- Added a credential-free CodexBar history reader for Claude, Codex, and a
  future Kimi history file. Pressure combines current use with projected use at
  reset; 85% entry and 70% exit thresholds provide deterministic hysteresis.
- Added persistent provider health and event ledgers. Canonical nonzero
  rate-limit exits now record a reset-derived or default 15-minute cooldown;
  repeated failures cannot shorten an existing cooldown and providers are
  reconsidered automatically after expiry.
- Added opt-in `AGENT_JOB_QUOTA_ROUTING`. It rebalances only default durable
  specialist routes, preserves explicit targets, refuses a rate-limited
  fallback, and leaves static routing intact when telemetry is stale or absent.
- Extended `route_status` with health, pressure, reset/cooldown evidence, and
  alerts. The rollout is independently reversible without deleting evidence or
  changing queued jobs.
- Verification before independent review: 217 unit/integration tests, Python
  compile checks, and diff whitespace validation pass.
- Kimi K3's first review found two ship blockers: broad stdout/stderr matching
  could falsely cooldown healthy providers, and fallback selection could move
  onto a provider under greater pressure. It also identified lazy env parsing,
  expired-window carryover, incomplete feature isolation, and retention/test
  gaps.
- Tightened normalization to bounded provider-specific stderr signatures with
  adjacent retry evidence, compare pressured providers before swapping, validate
  quota timing at startup, reject expired-window pressure, preserve hysteresis
  after cooldown, gate all behavior behind the rollout flag, validate health
  keys, and prune retained health events. Added focused regressions for each.
- Post-review verification passes all 224 unit/integration tests, Python compile
  checks, and diff whitespace validation.
- Kimi K3's targeted follow-up returned SHIP with no remaining blockers. Its one
  non-blocking note clarified that a rate-limited primary may legitimately use a
  pressured fallback; the operator documentation now states that exception.

## 2026-08-12 - Codex native routing canary P1

- Added an opt-in `codex_canary` policy that routes only focused, low/medium-risk,
  session-scoped Codex implementation, exploration, and test work to a native
  Spark worker. Claude, Kimi, Hermes, and all unsupported intents remain shadow;
  direct fallback is returned when cooperative capacity is full.
- Made decision persistence and native reservation admission one SQLite
  transaction. Added additive lifecycle columns, a default three-reservation
  machine limit, 15-minute TTL expiry, idempotent terminal feedback,
  session-scoped reconciliation, and decision-to-feedback join telemetry.
- Exposed `route_feedback`, `route_reconcile`, and `route_status` through the raw
  client, guarded CLI, review core, and MCP alongside `route_decide`.
- Added an installer-managed Codex `spark-worker` role using
  `gpt-5.3-codex-spark`, a default three-thread native machine ceiling, and a
  separate routing guidance block that does not overwrite customized provider
  policy. The primary Codex model remains unchanged by this work.
- Covered concurrent admission, capacity fallback, ownership, idempotency,
  conflicting feedback, reconciliation, expiry, telemetry, old-database
  migration, transport parity, installer behavior, and shadow compatibility.
  The full verification pass before assembly review completed 204 tests; the
  final post-review suite completed 210 tests successfully.
- Kimi K3 assembly review `4f8ecab3-89b9-4860-b5df-4f82b60b057b` found no
  blockers and recommended shipping. Addressed its capacity-accounting finding
  by retaining active reservations during prune, made admission truly
  machine-wide, preserved locally owned non-Codex guidance byte-for-byte, added
  contextual numeric environment errors, and clarified feedback-return telemetry.
- Live rollout caught a Codex 0.144.6 schema mismatch before merge. The stable
  `multi_agent` surface uses `agents.max_threads`; the similarly named
  `[features.multi_agent_v2]` setting is currently inert because v2 is disabled.
  Corrected the installer to migrate the misplaced key into the stable setting
  while preserving the chosen value, and removed non-config metadata from the
  worker profile.
- Strict Codex config loading and role/model resolution now pass. Codex CLI
  0.144.6 on this machine does not expose a native spawn tool with either the
  stable feature set or an ephemeral `multi_agent_v2` enablement, so cooperating
  callers must continue to report `native_subagents=false` on that surface. The
  canary then fails open to `direct`; no work is routed into a nonexistent lane.

## 2026-08-12 - Shadow routing protocol P0

- Added versioned `route_decide` protocol v1 across the supervisor socket,
  guarded review CLI, raw client CLI, and MCP surface. The protocol accepts
  structured task intent and surface capabilities, but P0 is explicitly
  shadow-only and cannot reserve, launch, submit, or alter existing jobs.
- Centralized a machine-readable copy of the current caller-based specialist
  routing table with stable provider-family model aliases. Explicit targets are
  represented, recursive delegation still resolves to a direct lane, and
  current policy does not automatically delegate implementation.
- Persisted bounded routing requests and responses in a separate SQLite
  `route_decisions` table with decision IDs, policy versions, reasons, and the
  existing 14-day retention policy. Existing `job_submit` behavior and schemas
  remain compatible.
- Added policy, validation, persistence, and CLI/MCP parity tests. The installed
  skill table remains authoritative during P0 so shadow decisions can be
  compared before the Codex-only enforcement canary; P1 will remove that
  temporary duplication.
- K3 independently returned a ship verdict with no blockers. Follow-up hardening
  canonicalizes persisted fields, rejects non-integer protocol versions, removes
  model aliases from direct routes, discards unknown fields before persistence,
  and covers retention and size boundaries.

## 2026-08-12 - Quote-safe provider process identity

- Traced four Kimi launch failures across the Mac mini and MacBook to process
  identity bookkeeping, not Kimi itself: the supervisor read the full macOS
  `ps command` value and passed it to `shlex.split`, so unmatched quotes in an
  ordinary prompt raised `No closing quotation` after the provider spawned.
- Replaced full-command parsing with the executable-only macOS `ps comm` field
  for launch bookkeeping and restart cleanup. Existing PID group, process start
  time, and resolved executable checks remain in place.
- Added regressions for both launch and restart cleanup using process arguments
  with an unmatched apostrophe. These tests fail against the prior parser and
  verify that quote-bearing prompts no longer block launch or orphan a process
  during supervisor restart.
- Local verification passes the full 178-test suite and compile checks. Opus 5
  independently confirmed the production fix and required strengthening both
  regression paths before shipping.

## 2026-08-11 - Kimi model normalization

- Moved Kimi model selection from advisory caller behavior into the supervisor
  boundary: omitted/default requests select K3, supported K3/K2.7 aliases are
  canonicalized, and stale or unknown Kimi aliases fall back to K2.7.
- Added `requested_model` persistence so operators can distinguish what a
  caller asked for from the effective model that ran. Idempotency hashes include
  both values, and fallback jobs expose a normalization message.
- Made the model optional only for Kimi across MCP, review CLI, raw client CLI,
  and delegated implementation; Claude and Codex continue to require one.
- Added integration coverage for defaults, legacy aliases, canonical variants,
  and non-Kimi validation. Opus review confirmed migration compatibility and
  identified default portability and silent fallback as immediate hardening;
  the default is now environment-overridable and fallback is operator-visible.
  Requested and effective models both participate in idempotency, and command-line
  clients retain early validation for missing Claude/Codex models.
- Both machines register the K3 canonical ID. Local verification passes all 176
  unit/integration tests. K3 review could not start because the Kimi account is
  quota-blocked; Opus 5 completed the fallback review and recommended shipping
  after the hardening above.
- PR #3 passed macOS CI and merged as `c27cf99`. The Mac mini and MacBook pulled
  that commit after confirming no launching or running jobs, restarted their
  LaunchAgents, refreshed all coding-client integrations, migrated the live
  SQLite schema with `requested_model`, and passed supervisor ping checks.

## 2026-08-10 - Standalone extraction

- Extracted the durable agent-job supervisor from the Hermes repository into a
  coding-agent-specific project.
- Added a repository-local Python environment and bootstrap flow.
- Added managed integrations for Codex, Claude Code/Desktop, Kimi Code, and
  optional Hermes profiles.
- Made bootstrap select a modern Homebrew Python when macOS still exposes Python
  3.9, and made LaunchAgent provider binary paths host-specific.
- Preserved the existing per-user supervisor state location so migration does
  not discard queued history or inbox deliveries.
- Kept native provider execution as the default; ACP/CAO remains optional.
- Added a macOS GitHub Actions gate for compile checks and the full test suite.
- Local verification: 169 unit/integration tests pass on macOS; independent
  Opus review initially returned HOLD on migration safety.
- Remediated the review blockers: preserved local routing policy and MCP timeout
  fields, made bootstrap checks read-only, refused supervisor swaps with active
  jobs, verified replacement PIDs, made Hermes migration globally transactional,
  and preserved profile timing settings.
- Added a migration/rollback runbook and fail-closed handling for symlinked
  client configuration. Follow-up review, two-machine deployment, and live smoke
  checks remain pending.
- Opus follow-up changed the release verdict to SHIP for publishing and found
  three deployment edge cases. Fixed idempotency with extra Codex env keys,
  duplicate Hermes env blocks, stable-path enforcement, and fail-closed active
  job detection. Deployment and smoke checks remain pending.
- Created the public `minhnkn22/atum-agent-jobs` repository. The implementation
  is being published through `feat/standalone-extraction`; `main` contains only
  the initial governance, license, and migration documentation until PR merge.
- Reproduced and fixed the cancellation test race noted by Opus: long-poll reads
  may wake on progress before terminal state, so the test now advances cursors
  through intermediate wakes as required by the protocol.
- The first CI run exposed a harness mismatch: dependencies were installed into
  the runner's global Python while the installer requires `.venv`. Updated CI to
  create and test through the same repository-local runtime used in production.
- PR #1 passed the macOS CI gate and merged as `59e689a`.
- Deployed `main` to `~/.local/share/atum-agent-jobs` on the Mac mini and
  MacBook. Both eight-target client checks are idempotent and both launchd
  services report the stable checkout path.
- Migrated twelve applicable Mac mini Hermes profiles as optional consumers;
  the MacBook had no applicable cross-agent Hermes registration. No live client
  or profile configuration on either host references the legacy runtime.
- Completed read-only Codex smoke jobs through each local supervisor with
  retained normalized results: `MINI_AGENT_JOBS_OK` and
  `MACBOOK_AGENT_JOBS_OK`.
- The old Hermes-side checkout remains temporarily as rollback material. It is
  no longer the active supervisor or client/profile integration source.
# 2026-08-12: Project root casing compatibility

- Added both `~/Projects` and `~/projects` to the shared workspace policy.
- This fixes rejected jobs when macOS resolves both spellings to the same
  directory but Python path containment compares the submitted spelling.
- Added a focused regression test and regenerated supervisor/client settings.
## 2026-08-13 - Private organization migration

- Transferred the repository from `minhnkn22/atum-agent-jobs` to the private
  `anka-ventures-labs/atum-code-orchestrator` repository.
- Kept the stable local install path, `agent-jobs` skill name, MCP interface,
  supervisor state paths, and runtime identifiers unchanged so existing coding
  surfaces continue to work after only their Git remote is updated.
- Established `anka-ventures-labs/atum-code` as the private catalog and
  integration umbrella. The orchestrator remains an independently versioned
  component rather than being folded into a source monorepo.
# 2026-08-13: Use the self-hosted Python toolchain

- Replaced `actions/setup-python` with the runner's managed `python3.11`.
- The macOS setup action hard-codes the GitHub-hosted `/Users/runner` tool cache
  and requires passwordless `sudo` when a version is not already cached, which
  is incompatible with the least-privilege Mac Mini runner service.
- Verified the runner has Homebrew Python 3.11 available; CI now checks that
  prerequisite explicitly before creating its isolated virtual environment.

## 2026-08-14 - Portable runner priority

- Replaced the Mac Mini hardware and macOS constraints with `portable-ci`.
- The capability prefers the dedicated ASUS Linux runner, then dynamically
  exposes the MacBook and Mac Mini while earlier runners are busy or offline.
- Preserved the managed `python3.11` contract. ASUS provides Python 3.11 through
  the same stable command name and runs the workflow under the non-sudo
  `gha-runner` account.
- Fork-origin pull requests remain rejected before self-hosted assignment.
## 2026-08-24: Enforce the Claude native tool surface

- Replaced permission-only Claude tool configuration with an explicit
  `--tools` availability list plus a deny list for shell, nested-agent, network,
  workflow, and notebook tools.
- Read-only jobs now expose only repository inspection tools; implementation
  jobs additionally expose file editing tools but still cannot execute commands
  or recursively delegate.
- Kept strict empty MCP configuration, disabled session persistence, and applied
  safe mode to both read-only and implementation runs so hooks and other
  customizations cannot create a separate command-execution path.
- Verified all 120 supervisor tests pass. A live Sonnet CLI probe initialized
  with only `Glob`, `Grep`, and `Read`, exposed no MCP servers, and returned
  `BASH_UNAVAILABLE` when instructed to invoke Bash.
- Opus independently confirmed the `--tools` mechanism and found follow-up gaps.
  Removed the unsupported `LS` name, expanded defense-in-depth denials across
  current and legacy tool names, restored the Codex read-only sandbox assertion,
  and clarified that native CLI tool restriction does not yet confine read or
  write paths. The optional CAO backend remains a separate policy contract.
- After the review fixes, all 280 tests pass. A live implement-mode probe exposed
  exactly `Edit`, `Glob`, `Grep`, `Read`, and `Write`, with no MCP servers.
- A final isolated implement probe created the expected temporary file under
  `acceptEdits` plus safe mode, then removed all probe artifacts. Opus's targeted
  follow-up found no new blockers and returned a `SHIP` verdict.
# 2026-08-24 - ACO and Hermes runtime boundary

- Confirmed the live Mac mini deployments remain separate: ACO uses
  `~/.local/share/atum-agent-jobs`, `com.atum.agent-job-supervisor`, and
  `~/.local/state/agent-job-supervisor`; Hermes uses its own checkout, service
  label, state directory, and profile configuration.
- Removed ACO's legacy `bootstrap.py --with-hermes` path and the Hermes profile
  migration utility so an ACO install cannot repoint `~/.hermes/profiles`.
- Kept Hermes caller and wire-protocol compatibility for the independently
  deployed Hermes supervisor. No live Hermes profile, process, service, or state
  was changed.
- Added regression coverage proving ACO bootstrap and client targets do not
  manage Hermes profiles. Updated current installation and rollback docs to make
  the ownership boundary explicit.
- Follow-up review returned SHIP and identified a remaining runtime authorization
  overlap. Removed Hermes-owned directories from ACO's default workspace roots,
  pinned that exclusion in policy tests, and cleaned stale migration/runbook
  language. Opus was attempted first as requested but failed before review with
  Anthropic HTTP 529; the enforced one-hop Kimi K3 fallback completed the review.

## 2026-08-24 - Delegated implementation write confinement

- Added kernel-enforced macOS write confinement around native Claude and Kimi
  implementation jobs. Writes are limited to the resolved submitted workdir and
  a mode-`0700` per-job runtime directory used as `TMPDIR` and removed at job end.
- Kept Codex on its native `workspace-write` sandbox. Rejected CAO implementation
  and unsupported native platforms until they can enforce an equivalent boundary.
- Added a real Seatbelt regression test that permits a workspace write while
  denying sibling and symlink-escape writes. Existing tool restrictions remain
  in force; this phase intentionally addresses writes, while full read isolation
  remains constrained by subscription CLI authentication stored outside the repo.
- Verified the actual Sonnet implementation adapter under the wrapper: `Write`
  created the requested file inside an isolated workspace, the requested absolute
  sibling write did not create a file, and the provider exited successfully.
- Full verification passes all 277 unit and integration tests on macOS.
- Opus held the first checkpoint after finding that a transient `sandbox-exec`
  process name could defeat restart cleanup, the CAO launch choke point lacked
  its own implementation rejection, and Kimi had not been exercised live.
- Restart cleanup now identifies the exact process group by PID, PGID, and start
  time rather than an executable name that changes across `exec`. The command
  builder rejects legacy queued CAO implementation jobs before launch.
- Runtime confinement now rejects a symlinked runtime root, removes stale runtime
  directories at startup, protects workspace Git metadata, and keeps cleanup
  failures from leaking in-memory scheduler state. Documentation now states the
  remaining read, network, authentication-refresh, and workspace-config risks.
- Kimi's first live probe failed before model invocation because it writes logs
  beneath `~/.kimi`. Implementation jobs now redirect `KIMI_SHARE_DIR` into the
  disposable runtime while reading the real config and credentials without write
  access; the live probe is repeated as part of this checkpoint.
- The retry exposed current Kimi CLI argument drift: structured output now
  requires print mode. The adapter pairs `--print` with `--output-format
  stream-json` so durable jobs reach model execution on the installed CLI.
- The next startup check found that current Kimi also replaced frontmatter
  markdown agent definitions with versioned YAML specs. Both ACO Kimi agents now
  use native YAML definitions, separate system prompts, explicit restricted tool
  classes, and an empty subagent registry.
- The confined Kimi adapter now reaches provider authentication. The machine's
  current Kimi login returns the same 401 both inside and outside ACO, and its
  local managed-model config currently exposes K2.7 but not the preferred K3
  alias; a successful model write probe remains blocked on external reauthentication.
- Verified both replacement Kimi agent specs with the installed Kimi parser:
  read-only exposes four file-inspection tools, implementation exposes six
  inspection/editing tools, and both expose zero subagents. The repository's
  complete 277-test suite passes under `.venv` after the review fixes.

## 2026-08-24 - Mediated delegated verification

- Closed the filesystem-confinement checkpoint after the requested Opus targeted
  re-review was attempted. Anthropic returned HTTP 529 after repeated API retries
  before reading the code; the enforced one-hop route returned the review to the
  originating Codex session, which verified the original blockers against the
  committed diff and closed routing feedback without weakening confinement.
- Added durable caller-approved check contracts for explicit implementation jobs.
  A caller may attach up to eight named argv lists; the delegated model receives
  only `run_check(name)` and cannot submit arbitrary command text.
- Added a thin local MCP command broker for Claude and Kimi. Checks run serially
  in the submitted workspace under a nested macOS Seatbelt profile with network
  denied, Git metadata read-only, sibling writes denied, provider credentials and
  proxy variables removed, bounded output, bounded time, and process cleanup.
- Kept arbitrary Bash, Git, package installation, deployment, dev servers,
  external messaging, and nested delegation unavailable. The caller still owns
  diff inspection, final verification, and all Git operations.
- Kimi now always receives an explicit isolated MCP config, empty when no checks
  are approved, preventing user-global MCP servers from widening its tool surface.
- Approved argv contracts are cleared from SQLite when the provider launches;
  only the private per-job runtime MCP config survives until job cleanup.
- Added macOS broker tests covering unknown-name rejection, credential scrubbing,
  workspace/Git/sibling confinement, network denial, and timeout termination,
  plus supervisor persistence and provider-adapter tests. The complete suite now
  passes all 281 tests under the repository `.venv`.
- Opus's targeted follow-up could not start because Anthropic returned HTTP 529.
  The enforced one-hop Kimi K3 review verified the remediation commit, closed
  the orphan-process, unmediated-Codex, and unverified-Kimi blockers, and returned
  `SHIP`. Codex recorded terminal routing feedback exactly once.

## 2026-08-24 - Deployment setting retention

- The reviewed release was overlaid into the fixed Mac mini install checkout
  after preserving the prior working-tree patch under the supervisor state
  directory. No Hermes checkout, profile, service, state, or process was changed.
- Live deployment exposed two installer defects. Launchd needed slightly more
  than the previous ten-second observation window even though the replacement
  daemon started successfully, and a plain reinstall discarded existing
  routing, quota, concurrency, and provider-binary overrides from the plist.
- The installer now retains only its known machine deployment overrides, with
  explicit command environment values taking precedence. Release-owned policy
  such as approved workspace roots is still recomputed so stale authorization
  does not survive an upgrade.
- Launchd stop/start observation now permits 30 seconds. Regression tests cover
  retained settings, explicit precedence, stale policy rejection, and the new
  transition budget.
- The Mac mini was recovered with `surface_canary`, quota routing, dynamic
  concurrency, three configured provider slots, and the current Kimi Code binary.
  Client bindings report current across Codex, Claude, Claude Desktop, and Kimi.
- Focused installer tests, Python compilation, `git diff --check`, and the full
  287-test suite pass on the Mac mini.

## 2026-08-24 - MacBook deployment parity

- Confirmed the MacBook supervisor queue was drained before replacement: no
  queued, launching, running, or possibly-stalled jobs were present.
- Preserved the prior installed checkout as a patch and untracked-file archive
  under the MacBook supervisor state directory before applying the current
  Mac mini release overlay.
- Reinstalled the supervisor and all Codex, Claude, Claude Desktop, and Kimi
  bindings. Migrated the legacy Codex-only native reservation variable to the
  shared `AGENT_JOB_NATIVE_RESERVATIONS=3` setting while retaining the MacBook's
  provider binary paths and other known deployment overrides.
- The MacBook's 256-file shell limit caused false `Too many open files` failures
  in the full suite. With a 4096-file test limit, all 298 tests passed. This is a
  test-runner environment constraint; the launchd supervisor remained healthy.
- Verified supervisor ping, current client bindings, `surface_canary` protocol
  v2 routing, quota-aware provider health, dynamic three-slot configuration, and
  terminal feedback for a no-run routing smoke decision. Both coding desktop
  applications were restarted to reload their MCP configuration.

## 2026-08-24 - Kimi CLI generation compatibility

- Diagnosed four MacBook ACO failures after Kimi auto-upgraded to the Node-based
  CLI 0.31.1. The supervisor was still passing the legacy Python CLI's
  `--mcp-config-file` option, so jobs exited before model execution.
- Added capability-based CLI generation detection. Legacy installations retain
  their YAML agent, print-mode streaming, and explicit MCP arguments; modern
  installations use Markdown tool policies and prompt-mode JSON streaming.
- Modern jobs receive a private per-job `KIMI_CODE_HOME`, empty MCP declaration,
  and empty Skills directory while reusing only local Kimi authentication state.
  The runtime is removed by the existing terminal cleanup path.
- The first launchd smoke test showed that invoking modern Kimi `--help` can
  block behind its updater long enough to fail capability detection. Official
  `.kimi-code/bin/kimi` installs are now identified without spawning the CLI;
  nonstandard binary paths use the faster `--version` contract as fallback.
# 2026-08-28 - Adopt CI lifecycle policy v1

- Adopted `atum-platform/atum-code` policy `ci-policy/v1` for the bounded
  Python verification workflow.
- Superseded pull-request runs cancel within the workflow, while `main` runs
  remain serialized. Explicit PR transitions include draft/ready changes.
- Pinned GitHub Actions dependencies and added weekly Actions Dependabot.
- Verify with `actionlint`, local unit tests, and exact-head hosted checks.
