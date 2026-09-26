# Keep ACO's own credentials out of approved checks

Date: 2026-09-26

## Why

Approved checks run caller-approved commands, such as a test suite, for
delegated Claude implementation jobs. The model can edit the code those
commands run. The check sandbox denied reads of a fixed list of CLI credential
homes but not ACO's own: `opencode.env` (the OpenCode Go key) and the
implementation token in the supervisor state directory, the other
`AGENT_JOB_PROFILE_ENV` files (Claude credentials on the MacBook), OpenCode's
`auth.json`, and the leftover `~/.kimi-code` login. A check could print any of
them into the model's context. The Kimi removal session log recorded this gap.

## What changed

- The supervisor passes `ACO_CHECKS_DENY_READ` (a JSON list of absolute paths) to
  the check server: its state directory, the implementation token,
  `~/.local/share/opencode`, `~/.kimi-code`, and each profile file.
- The check server turns each path into a `deny file-read*` rule, then
  re-allows reads of the job's runtime directory, which lives inside the state
  directory. Seatbelt applies the later rule, which was confirmed with
  `sandbox-exec` before relying on it. Relative or malformed entries stop the
  check server from starting.

## Review

OpenCode (Muse Spark) review `ed9abead-db55-4616-aafa-f8c05e3ab5ba` returned HOLD
on two points:

- It asked whether the runtime directory, which is writable and re-allowed
  inside the denied state directory, could reach denied files through links.
  Tested directly: Seatbelt checks a symlink's resolved target and refuses to
  create a hardlink to, or copy, a denied file. A sibling job's runtime stays
  unreadable. The check-server test now asserts all of these.
- The check server trusted the supervisor to pass canonical paths. A denial
  given through a symlinked parent did not match, and the file leaked (the
  supervisor resolves its paths, so production was not exposed). The check
  server now resolves every entry itself; a test passes the state directory
  through a symlinked alias.

Also added: malformed-input cases (non-string, non-list, invalid JSON) and
supervisor assertions for `~/.kimi-code` and the implementation token.
`checks-mcp.json` in the runtime directory stays readable to the check; it
holds the check argv and the denial list, not credentials.

## Verification

- New check-server test: a check cannot read a denied state file or profile
  file, but can still write and read its runtime directory and workdir. The same
  check without the denial list prints both secrets.
- The supervisor test asserts that the generated check config names the state
  directory, the profile file, and OpenCode's data directory.
- Full suite: 330 tests pass.
