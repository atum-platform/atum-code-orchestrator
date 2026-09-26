# OpenCode as a caller surface; Kimi dropped from every surface

Date: 2026-09-26

## Why

The owner asked to propagate the OpenCode-based ACO to both Macs and to every
coding surface (Codex, Claude, OpenCode), to drop instructions that hard-code
Kimi, to run OpenCode Go with Muse Spark as the default, and to have CodexBar
track the OpenCode Go subscription instead of Kimi. This change also carries the
two follow-ups from the first live OpenCode review.

## What changed

- Follow-ups: the `opencode` rate-limit pattern now needs usage, period, or credit
  wording, so "context limit" or output-token errors no longer start a cooldown.
  The `opencode --version` probe allows 60 s instead of 10 (one cold start under
  launchd's background priority timed out) and never checks for updates.
- OpenCode is a caller surface. Routing gives it its own identity (`opencode` on
  the `opencode` surface, enforced under `surface_canary`); policy and capability
  matrix version `2026-09-26.2`. Because an OpenCode caller may run any model
  family, it routes code review to Codex then Claude, thinking work to Claude then
  Codex, and never to itself.
- The client installer registers `agent-jobs` in OpenCode's global config
  (`mcp`, type `local`) and adds a managed `~/.config/opencode/agent-jobs.md`
  through `instructions`. It deliberately does not create a global `AGENTS.md`,
  which would switch off OpenCode's fallback to `~/.claude/CLAUDE.md`. Configs
  with comments fail closed rather than lose them.
- Kimi Code is no longer a managed surface, and `kimi` is no longer a routing,
  MCP, review-CLI, client, or delegation target. Kimi K3 remains reachable as
  `opencode-go/kimi-k3`. The dormant Kimi launch code stays until a removal PR.
- The skill, its operations reference, README, client-integration, migration, and
  supervisor docs no longer describe Kimi as a target or surface.
- The quota broker reads CodexBar's `opencodego` history for the `opencode`
  provider, so Go's five-hour, weekly, and monthly windows drive routing pressure.

## CodexBar

On the MacBook, `CodexBarCLI config set-api-key --provider opencodego --stdin`
enabled OpenCode Go from the owner-only ACO key file without exposing the key,
and `config disable --provider kimi` turned off Kimi. A live read reported the Go
windows at 46% (five-hour), 18% (weekly), and 9% (monthly), almost entirely one
US$1.37 Kimi K3 review against K3's small allowance.

## Review

The cross-family review ran through the live router on OpenCode with Muse Spark
(job `5bcdba26`, about US$0.011, 19 tool calls). Verdict SHIP. The following
findings were fixed and tested before merge:
- An OpenCode caller running a GPT or Claude model could be routed to the
  same-family target. Callers now pass an optional `caller_model`, and routing
  drops the target that shares its family.
- The raw supervisor socket still accepted `kimi`, and `surface_canary` still
  enforced the `kimi-code` surface. Kimi no longer has a provider slot, a caller
  identity, or a surface.
- The rate-limit pattern missed "exceeded your weekly limit" and plural "limits".
- Having both `opencode.json` and `opencode.jsonc`, or a non-object
  `environment`, now fails closed.
- The supervisor doc still described Kimi defaults, surfaces, and CodexBar files
  as current; those passages now describe OpenCode.

Verified as no change needed: the 60 s version probe only runs inside the command
builder, which executes on a worker thread, so it cannot stall the event loop.
