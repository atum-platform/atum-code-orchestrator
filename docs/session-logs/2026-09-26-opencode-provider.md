# Replace Kimi with OpenCode

Date: 2026-09-26

## Why

- The Kimi lane stopped working on 2026-09-12. Its last successful job ran at
  12:46 local time that day; since then every Kimi job has failed, 24 of them
  with `provider.auth_error 403 Your current subscription does not have access
  to Kimi Code`. In the preceding 30 days, 57 of 59 read-only Kimi jobs failed.
- With Codex often quota-exhausted, Claude callers were left with no
  cross-family reviewer: a routed review fell back to `direct`.
- The owner chose OpenCode as the replacement: the Go subscription (US$10/month)
  includes Muse Spark 1.3 Contributor (Meta) with a US$60 monthly allowance and
  Kimi K3 with US$15, and it explicitly supports third-party coding agents. The
  owner accepts that Muse Spark Contributor trains on prompts and completions.

## Spike evidence

Verified with Homebrew OpenCode 1.18.32 and OpenCode Desktop's bundled 2.0.18
CLI, in disposable repositories under a throwaway home:

- A repository's `.opencode/plugin/*.js` runs on any OpenCode startup, including
  `opencode debug config`. `--pure` stops it; `OPENCODE_DISABLE_DEFAULT_PLUGINS`
  does not.
- A repository `opencode.json` MCP server survives both an inline
  `OPENCODE_CONFIG_CONTENT` with `"mcp": {}` and `--pure`. Only keeping the file
  away from OpenCode prevents it.
- Config discovery stops at the Git root, and a non-Git directory does not look
  above itself. `XDG_*` relocation moves config, data, cache, and state.
- Permission rules are ordered and the last match wins. With the job's rules
  layered last, `doom_loop` and `external_directory` resolve to deny and the only
  effective tools are read, glob, and grep.
- `opencode run` reads stdin to end of file whenever stdin is not a terminal; an
  open stdin hangs before any network call.
- Anonymous free-tier models return HTTP 403 `FreeTierError` ("can only be used
  from within OpenCode") to headless `run`, with either CLI generation, isolated
  or against the real user data. The interactive Desktop app uses them without a
  key. Impersonating the Desktop client was rejected.
- The 1.x source (`packages/opencode/src/cli/cmd/run.ts` at `v1.18.32`) emits
  `text`, `tool_use`, `step_start`, `step_finish`, `reasoning`, and `error`
  records, auto-rejects permission requests in headless mode, and exits 1 after
  any session error.
- The 2.x CLI in OpenCode Desktop changes the contract (`--standalone`, a shared
  background service by default, no `--pure`), so ACO accepts only 1.x.

## What changed

- New `opencode` provider: read-only, run in a copy-on-write staged copy of the
  workdir's Git-visible files without OpenCode config, `.env` files, keys, or
  symlinks; private `HOME`/XDG; deny-by-default permissions; stdin prompt;
  JSON event normalization that keeps tool names and sizes but never tool
  content; error-event-only rate-limit scanning; `--print-logs --log-level ERROR`.
- Models default to `opencode-go/muse-spark-1.3-contributor`, must match the
  `opencode-go/` prefix so the key cannot draw pay-as-you-go credits, and a
  default from a caller family is refused. Because one key serves every OpenCode
  provider and titles or compaction use a separate small model, jobs also enable
  only the allowed providers, pass a fixed `--title`, and pin `small_model`.
- Language servers and formatters are disabled, `TMPDIR` lives in the job
  runtime, and the command builder runs on a worker thread so staging a large
  repository cannot block the supervisor's event loop.
- Routing: OpenCode takes every slot Kimi held. Engineering work lost its
  automatic fallback because OpenCode is read-only. Cross-family checks for
  explicit OpenCode models use the model's family. Kimi stays accepted only as an
  explicit target. Policy and capability-matrix version `2026-09-26.1`.
- Quota broker tracks `opencode` so its cooldowns reach routing health.
- `AGENT_JOB_PROFILE_ENV` accepts a path list. The installer persists OpenCode
  settings and never discovers or pins the OpenCode binary.
- Client guidance defaults name OpenCode, and the migration table now recognizes
  every earlier installer default instead of only one.

## Verification

- 330 tests pass on Python 3.11.16; 13 are new.
- End to end against the real 1.18.32 CLI, driven by the supervisor's own command
  builder on a repository containing a marker-writing `.opencode` plugin and
  `opencode.json` MCP server: neither marker was created, the staged copy held
  only the source file, and the run ended with exit 1 and a parsed error event.
  Without a key the cause was `ProviderModelNotFoundError`, because OpenCode only
  activates the Go provider when `OPENCODE_API_KEY` is present.
- Cross-family review was unavailable: the enforced route chose Kimi, whose job
  `67402053` failed with the same subscription 403, and the one-hop escalation
  returned `direct` because Codex was quota-exhausted. A direct adversarial
  review found the small-model billing path, LSP/formatter execution, temporary
  file residue, and event-loop blocking above; each is fixed and covered.
- Not yet verified: an authenticated Go review. The agent session was not
  permitted to read secrets from Infisical, so the key must be materialized by an
  operator (see `docs/MIGRATION.md`).

## Follow-ups

- Authenticated end-to-end review on the MacBook, then deploy on the Mac mini.
- Apply client guidance with `tools/install_agent_job_clients.py --apply` in a
  window with the desktop apps closed.
- Remove the Kimi provider code once OpenCode has handled real reviews.
- Workspace-confined OpenCode implementation mode.
- Support the 2.x CLI contract once it ships as a public release.
