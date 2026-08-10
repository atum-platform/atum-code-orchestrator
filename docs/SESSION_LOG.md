# Session Log

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
- Local verification: 167 unit/integration tests pass on macOS; independent
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
