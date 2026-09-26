# OpenCode reasoning effort

Date: 2026-09-26

## Why

The owner noticed that ACO callers could not choose how hard OpenCode models
think, and asked for `xhigh` by default. ACO never passed OpenCode's `--variant`,
so Muse Spark 1.3 ran at its server default. Its published benchmarks (Vals AI,
Artificial Analysis) use `xhigh`. Earlier default reviews used 3k to 12k
reasoning tokens.

## What changed

- `reasoning_effort` on job submission: the MCP `job_submit` tool,
  `--reasoning-effort` on the review CLI and raw client, and the supervisor
  socket. It accepts `minimal`, `low`, `medium`, `high`, `xhigh`, or `max` and is
  refused for Claude and Codex.
- The supervisor resolves the level at launch. An explicit request comes first,
  then `AGENT_JOB_OPENCODE_DEFAULT_REASONING_EFFORT` (default `xhigh`, which the
  installer persists). The level is checked against the model's variants from
  `opencode models <provider> --verbose`, cached for 24 hours in
  `opencode-models.json`. An explicit level the model lacks fails the job with the
  offered levels; the default is dropped for such models (Kimi K3 offers only
  `max`).
- Jobs record the request (`reasoning_effort`) and what ran
  (`effective_reasoning_effort`). Only explicit levels join the idempotency hash,
  so retries of older jobs still match.

## Findings

- OpenCode 1.18.32 silently accepts an unknown `--variant` (`bogus` ran without
  error), so the catalog check is required. Without it, a level the model lacks
  would quietly run at the default.
- OpenCode stores a CLI variant in `~/.local/state/opencode/model.json`. ACO's
  per-job private home discards that file.
- Single-prompt reasoning token counts varied more between runs than between
  levels, so the level was verified from OpenCode's source and ACO's argv rather
  than from token counts.

## Verification

- Unit tests cover default resolution, dropping the default, explicit
  mismatches, an unavailable catalog, stale-cache fallback, parsing the verbose
  listing, submit validation, stored and effective values, and idempotency.
  Full suite: 336 tests pass.
- End to end with a throwaway supervisor on this branch and real OpenCode Go:
  a default job ran with `--variant xhigh` and recorded `xhigh`; `max` on Muse
  Spark failed at launch, naming the offered levels; the cached catalog listed
  30 models, with Kimi K3 offering only `max`.
