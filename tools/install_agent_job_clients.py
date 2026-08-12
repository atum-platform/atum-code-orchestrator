#!/usr/bin/env python3
"""Transactionally install agent-jobs bindings for local coding clients."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from json import JSONDecodeError
import os
from pathlib import Path
import shutil
import stat
import tempfile

from agent_job_policy import allowed_roots_value


REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = REPO_ROOT / "tools" / "agent_jobs_server.py"
SKILL_SOURCE = REPO_ROOT / "skills" / "agent-jobs"
CODEX_SPARK_WORKER = REPO_ROOT / "clients" / "codex" / "spark-worker.toml"
PYTHON_PATH = REPO_ROOT / ".venv" / "bin" / "python"
GUIDANCE_START = "<!-- AGENT_JOBS_GUIDANCE_START -->"
GUIDANCE_END = "<!-- AGENT_JOBS_GUIDANCE_END -->"
CODEX_ROUTING_START = "<!-- AGENT_JOBS_CODEX_ROUTING_START -->"
CODEX_ROUTING_END = "<!-- AGENT_JOBS_CODEX_ROUTING_END -->"
LEGACY_SKILL_ROOTS = (
    Path.home() / ".local/share/hermes-agent-review-sidecars/skills/agent-jobs",
)

GUIDANCE = {
    "Codex guidance": """## Agent Jobs

Use `$agent-jobs` for cross-agent reviews, consultations, planning, architecture,
UI/UX, visual design, product judgment, copywriting, research, or explicitly
delegated implementation. Use Opus first for planning, architecture, design,
product, copy, and research. Use Kimi first for code review, then Opus only on
provider failure or quota exhaustion. Never delegate recursively or send secrets.
Save durable job IDs and cursors, treat `possibly_stalled` as alive but quiet,
and leave `max_turns=0` unless a bounded turn ceiling is explicitly required.
Codex owns local inspection, implementation unless delegated, tests, docs, and
the final decision. Read the retained result before acknowledging inbox delivery.
""",
    "Claude guidance": """## Agent Jobs

Use `$agent-jobs` for durable cross-agent review and delegation. As a Claude
caller, use Codex first for code review, planning, and implementation; use Kimi
only as the documented fallback. Never delegate back to Claude, recurse, or send
secrets. Run the skill's guarded CLI, retain job IDs and cursors, treat
`possibly_stalled` as alive but quiet, and leave `max_turns=0` by default.
Verify returned advice and changes locally before accepting them.
""",
    "Kimi guidance": """## Agent Jobs

Use `$agent-jobs` for durable cross-agent review and delegation. As a Kimi
caller, use Codex first for code review and Opus first for planning, design,
product, copy, and research. Never delegate back to Kimi, recurse, or send
secrets. Retain job IDs and cursors, treat `possibly_stalled` as alive but quiet,
and leave `max_turns=0` by default. Verify all returned work locally.
""",
}

CODEX_ROUTING_GUIDANCE = """## Codex Routing Canary

For focused implementation, exploration, or test work that is separable from the
primary task, call the agent-jobs `route_decide` tool before spawning a native
subagent. Pass a stable task/session ID and `native_subagents=true`. Follow the
returned lane only when `enforced=true`; shadow responses are telemetry. A
`native_subagent` lane means spawn one worker using the returned worker profile
and model alias, retain the decision ID, and send `route_feedback` exactly once
when it completes, fails, is abandoned, or is escalated. On a resumed task, call
`route_reconcile` with that session's still-active decision IDs. The routing tool
does not spawn or terminate native agents; Codex remains responsible for their
lifecycle, integration, verification, and the final result.
"""


def python_path(home: Path | None = None) -> Path:
    del home
    return PYTHON_PATH


def server_config(home: Path | None = None) -> dict[str, object]:
    resolved_home = (home or Path.home()).expanduser().resolve()
    return {
        "command": str(PYTHON_PATH),
        "args": [str(SERVER_PATH)],
        "env": {"AGENT_JOB_ALLOWED_ROOTS": allowed_roots_value(resolved_home)},
    }


def _atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600 if mode is None else mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _backup_path(path: Path, suffix: str) -> Path:
    return path.with_name(f"{path.name}.bak.agent-jobs-{suffix}")


def _backup(path: Path, suffix: str) -> Path | None:
    if not path.exists() and not path.is_symlink():
        return None
    backup = _backup_path(path, suffix)
    if backup.exists() or backup.is_symlink():
        raise FileExistsError(f"Backup already exists: {backup}")
    if path.is_symlink():
        backup.symlink_to(os.readlink(path))
    elif path.is_dir():
        shutil.copytree(path, backup)
    else:
        shutil.copy2(path, backup)
    return backup


def merge_mcp_config(path: Path, suffix: str, apply: bool, home: Path | None = None) -> bool:
    if path.is_symlink():
        raise ValueError(f"Refusing to replace symlinked config; update its target explicitly: {path}")
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in MCP config {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"MCP config must contain a JSON object: {path}")
    else:
        data = {}
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"mcpServers must be a JSON object: {path}")
    desired = server_config(home)
    current = servers.get("agent-jobs")
    if current is not None and not isinstance(current, dict):
        raise ValueError(f"agent-jobs must be a JSON object: {path}")
    merged = dict(current or {})
    merged["command"] = desired["command"]
    merged["args"] = desired["args"]
    merged_env = dict(merged.get("env") or {})
    merged_env.update(desired["env"])
    merged["env"] = merged_env
    if current == merged:
        return False
    servers["agent-jobs"] = merged
    if apply:
        _backup(path, suffix)
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n", mode)
    return True


def merge_codex_config(path: Path, suffix: str, apply: bool, home: Path | None = None) -> bool:
    if path.is_symlink():
        raise ValueError(f"Refusing to replace symlinked config; update its target explicitly: {path}")
    try:
        import tomlkit
    except ImportError as exc:
        raise RuntimeError("tomlkit is missing; run bootstrap.py first") from exc
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        document = tomlkit.parse(original)
    except Exception as exc:
        raise ValueError(f"Invalid TOML in Codex config {path}: {exc}") from exc
    servers = document.setdefault("mcp_servers", tomlkit.table())
    desired = server_config(home)
    current = servers.get("agent-jobs")
    managed_current = None if current is None else {
        "command": current.get("command"),
        "args": list(current.get("args") or []),
        "env": {
            key: (current.get("env") or {}).get(key)
            for key in desired["env"]
        },
    }
    has_timeouts = current is not None and "startup_timeout_sec" in current and "tool_timeout_sec" in current
    table = current if current is not None else tomlkit.table()
    table["command"] = desired["command"]
    table["args"] = desired["args"]
    if "startup_timeout_sec" not in table:
        table["startup_timeout_sec"] = 30.0
    if "tool_timeout_sec" not in table:
        table["tool_timeout_sec"] = 120.0
    existing_env = table.get("env")
    env = existing_env or tomlkit.table()
    for key, value in desired["env"].items():
        env[key] = value
    if existing_env is None:
        table.add("env", env)
    servers["agent-jobs"] = table
    agents = document.setdefault("agents", tomlkit.table())
    if "max_concurrent_threads_per_session" not in agents and "max_threads" not in agents:
        agents["max_concurrent_threads_per_session"] = 3
    role = agents.get("spark-worker") or tomlkit.table()
    role["description"] = (
        "Fast Codex worker for one focused implementation, exploration, or test scope."
    )
    role["config_file"] = str(CODEX_SPARK_WORKER)
    agents["spark-worker"] = role
    updated = tomlkit.dumps(document)
    if managed_current == desired and has_timeouts and updated == original:
        return False
    if apply:
        _backup(path, suffix)
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        _atomic_write(path, updated, mode)
    return True


def _managed_guidance(name: str) -> str:
    return f"{GUIDANCE_START}\n{GUIDANCE[name].rstrip()}\n{GUIDANCE_END}"


def merge_guidance(path: Path, name: str, suffix: str, apply: bool) -> bool:
    if path.is_symlink():
        raise ValueError(f"Refusing to replace symlinked guidance; update its target explicitly: {path}")
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    if original.count(GUIDANCE_START) > 1 or original.count(GUIDANCE_END) > 1:
        raise ValueError(f"Duplicate agent-jobs guidance markers: {path}")
    start = original.find(GUIDANCE_START)
    end = original.find(GUIDANCE_END)
    if (start == -1) != (end == -1) or (start != -1 and end < start):
        raise ValueError(f"Malformed agent-jobs guidance markers: {path}")
    managed = _managed_guidance(name)
    if start != -1:
        # Existing marked guidance is locally owned policy. Provider availability
        # overrides and team-specific routing must not be silently overwritten.
        if name != "Codex guidance":
            return False
        updated = original
    else:
        # Adopt legacy policy verbatim; the shared skill carries portable defaults.
        import re
        match = re.search(r"(?ms)^## Agent Jobs\s*\n.*?(?=^##\s|\Z)", original)
        if match:
            section = match.group(0).rstrip()
            adopted = f"{GUIDANCE_START}\n{section}\n{GUIDANCE_END}"
            updated = original[:match.start()] + adopted + "\n\n" + original[match.end():].lstrip("\n")
        else:
            updated = (managed + "\n\n" + original.lstrip()) if original else managed
    if name == "Codex guidance":
        if original.count(CODEX_ROUTING_START) > 1 or original.count(CODEX_ROUTING_END) > 1:
            raise ValueError(f"Duplicate Codex routing guidance markers: {path}")
        route_start = updated.find(CODEX_ROUTING_START)
        route_end = updated.find(CODEX_ROUTING_END)
        if (route_start == -1) != (route_end == -1) or (
            route_start != -1 and route_end < route_start
        ):
            raise ValueError(f"Malformed Codex routing guidance markers: {path}")
        route_block = (
            f"{CODEX_ROUTING_START}\n{CODEX_ROUTING_GUIDANCE.rstrip()}\n{CODEX_ROUTING_END}"
        )
        if route_start == -1:
            updated = updated.rstrip() + "\n\n" + route_block + "\n"
        else:
            route_end += len(CODEX_ROUTING_END)
            updated = updated[:route_start] + route_block + updated[route_end:]
    updated = updated.rstrip() + "\n"
    if updated == original:
        return False
    if apply:
        _backup(path, suffix)
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        _atomic_write(path, updated, mode)
    return True


def merge_kimi_guidance(path: Path, suffix: str, apply: bool) -> bool:
    return merge_guidance(path, "Kimi guidance", suffix, apply)


def _recognized_legacy_skill(destination: Path) -> bool:
    if not destination.is_symlink():
        return False
    try:
        target = destination.resolve()
    except OSError:
        return False
    return target in {path.expanduser().resolve() for path in LEGACY_SKILL_ROOTS}


def ensure_skill_link(destination: Path, apply: bool) -> bool:
    if destination.is_symlink() and destination.resolve() == SKILL_SOURCE.resolve():
        return False
    if (destination.exists() or destination.is_symlink()) and not _recognized_legacy_skill(destination):
        raise FileExistsError(f"Refusing to replace unrecognized skill path: {destination}")
    if apply:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            destination.unlink()
        destination.symlink_to(SKILL_SOURCE)
    return True


def _paths(home: Path) -> dict[str, Path]:
    return {
        "shared skill": home / ".agents/skills/agent-jobs",
        "Claude skill": home / ".claude/skills/agent-jobs",
        "Codex MCP": home / ".codex/config.toml",
        "Codex guidance": home / ".codex/AGENTS.md",
        "Claude guidance": home / ".claude/CLAUDE.md",
        "Claude Desktop MCP": home / "Library/Application Support/Claude/claude_desktop_config.json",
        "Kimi MCP": home / ".kimi-code/mcp.json",
        "Kimi guidance": home / ".kimi-code/AGENTS.md",
    }


def _operation(name: str, path: Path, suffix: str, apply: bool, home: Path) -> bool:
    if name in {"shared skill", "Claude skill"}:
        return ensure_skill_link(path, apply)
    if name == "Codex MCP":
        return merge_codex_config(path, suffix, apply, home)
    if name.endswith("guidance"):
        return merge_guidance(path, name, suffix, apply)
    return merge_mcp_config(path, suffix, apply, home)


def _validate_runtime() -> None:
    for path in (PYTHON_PATH, SERVER_PATH, SKILL_SOURCE / "SKILL.md"):
        if not path.is_file():
            raise FileNotFoundError(f"Required agent-jobs runtime path is missing: {path}")


def _plan(home: Path) -> tuple[dict[str, bool], dict[str, str]]:
    changes, errors = {}, {}
    for name, path in _paths(home).items():
        try:
            changes[name] = _operation(name, path, "preview", False, home)
        except Exception as exc:
            changes[name] = False
            errors[name] = str(exc)
    return changes, errors


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def apply_changes(home: Path, changes: dict[str, bool], suffix: str) -> None:
    paths = _paths(home)
    existed = {name: path.exists() or path.is_symlink() for name, path in paths.items()}
    for name, path in paths.items():
        if changes[name] and existed[name] and (_backup_path(path, suffix).exists() or _backup_path(path, suffix).is_symlink()):
            raise FileExistsError(f"Backup already exists: {_backup_path(path, suffix)}")
    completed: list[str] = []
    try:
        for name, path in paths.items():
            if changes[name]:
                if name in {"shared skill", "Claude skill"} and existed[name]:
                    _backup(path, suffix)
                completed.append(name)
                _operation(name, path, suffix, True, home)
    except Exception:
        for name in reversed(completed):
            path = paths[name]
            backup = _backup_path(path, suffix)
            if backup.is_symlink():
                _remove_path(path)
                path.symlink_to(os.readlink(backup)); backup.unlink()
            elif backup.is_dir():
                _remove_path(path)
                shutil.move(str(backup), path)
            elif backup.exists():
                _remove_path(path)
                shutil.copy2(backup, path); backup.unlink()
            elif not existed[name]:
                _remove_path(path)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true")
    action.add_argument("--check", action="store_true")
    parser.add_argument("--backup-suffix", default="")
    parser.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    home = args.home.expanduser().resolve()
    suffix = args.backup_suffix or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    try:
        _validate_runtime()
        changes, errors = _plan(home)
        if errors:
            for name in changes:
                print(f"{name}: {'error: ' + errors[name] if name in errors else 'would update' if changes[name] else 'current'}")
            return 2
        if args.apply:
            apply_changes(home, changes, suffix)
    except Exception as exc:
        print(f"install: error: {exc}")
        return 2
    verb = "updated" if args.apply else "would update"
    for name, changed in changes.items():
        print(f"{name}: {verb if changed else 'current'}")
    return 1 if args.check and any(changes.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
