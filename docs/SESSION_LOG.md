# Session Log

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
