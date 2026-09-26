# Codex binary after a ChatGPT app update

Date: 2026-09-26

## Symptom

CodexBar on the MacBook showed Codex as unavailable: "Codex auth.json needs
refresh. CodexBar will retry through the Codex CLI".

## Cause

ChatGPT 26.924 (updated 2026-09-26 21:08) moved its bundled CLI from
`Contents/Resources/codex` to `Contents/Resources/codex-cli/bin/codex`.
`~/.local/bin/codex` and `/opt/homebrew/bin/codex` still pointed at the old path,
so CodexBar had no CLI to refresh through. The ID token in `~/.codex/auth.json`
had expired on 2026-09-18, although the access token was valid until
2026-09-28.

ACO's LaunchAgent also pinned `AGENT_JOB_CODEX_BIN` to the old resolved bundle
path. At runtime the supervisor skips a missing pin and falls back to `PATH`,
so jobs worked once the links were fixed. The installer, however, kept the dead
pin on every reinstall, and it resolved launcher links to bundle paths in the
first place.

## Fix

- Operations (MacBook): repointed both links to the new bundle path. After that,
  `codex login status` reported ChatGPT login, `CodexBarCLI usage --provider
  codex` succeeded, and CodexBar wrote a fresh `codex.json` after a restart. The
  Mac mini runs the npm Codex CLI and was unaffected.
- ACO: `_provider_binary` keeps launcher symlinks unresolved and rediscovers a
  retained path that no longer exists. The runtime candidate list adds the new
  bundle path as a last resort.

## Note

CodexBar showed the weekly Codex window at 100%, which is why routing has sent
Codex's usual slots elsewhere.

## Verification

- New installer test: a missing retained path is rediscovered as the launcher
  link, a valid retained path is kept, and an explicit link is not resolved.
- Full suite: 340 tests pass.
