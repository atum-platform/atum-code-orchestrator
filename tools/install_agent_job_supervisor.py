#!/usr/bin/env python3
"""Install or remove the per-user launchd service for the agent job supervisor."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import secrets
import shutil
import socket
import subprocess
import sys
import time

from agent_job_policy import allowed_roots_value


LABEL = "com.atum.agent-job-supervisor"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
STATE_DIR = Path.home() / ".local" / "state" / "agent-job-supervisor"
SUPERVISOR = Path(__file__).resolve().with_name("agent_job_supervisor.py")
IMPLEMENT_TOKEN_PATH = STATE_DIR / "implement.token"


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=check)


def _socket_request(payload: dict[str, object]) -> dict[str, object] | None:
    socket_path = STATE_DIR / "supervisor.sock"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(str(socket_path))
            client.sendall((json.dumps(payload) + "\n").encode())
            client.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = client.recv(64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            response = json.loads(b"".join(chunks).decode())
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not response.get("ok"):
        return None
    return dict(response.get("result") or {})


def _active_jobs() -> list[dict[str, object]]:
    active: list[dict[str, object]] = []
    for status in ("launching", "running"):
        result = _socket_request({"action": "list", "status": status, "limit": 200})
        if result is None:
            raise RuntimeError("Cannot verify active jobs from the existing supervisor; refusing replacement")
        active.extend(result.get("jobs") or [])
    return active


def _provider_binary(env_name: str, command: str, candidates: tuple[Path, ...]) -> str:
    override = os.environ.get(env_name, "").strip()
    if override:
        return str(Path(override).expanduser().resolve())
    discovered = shutil.which(command)
    if discovered:
        return str(Path(discovered).resolve())
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    return str(candidates[0])


def _service_environment() -> dict[str, str]:
    home = Path.home()
    environment = {
        "HOME": str(home),
        "USER": home.name,
        "LOGNAME": home.name,
        "SHELL": os.environ.get("SHELL", "/bin/zsh"),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "__CF_USER_TEXT_ENCODING": os.environ.get("__CF_USER_TEXT_ENCODING", "0x1F5:0x0:0x0"),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "PATH": os.pathsep.join((str(home / ".local/bin"), str(home / ".kimi-code/bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")),
        "AGENT_JOB_STATE_DIR": str(STATE_DIR),
        "AGENT_JOB_ALLOW_IMPLEMENT": "1",
        "AGENT_JOB_IMPLEMENT_TOKEN_FILE": str(IMPLEMENT_TOKEN_PATH),
        "AGENT_JOB_ALLOWED_ROOTS": os.environ.get(
            "AGENT_JOB_ALLOWED_ROOTS", allowed_roots_value(),
        ),
        "AGENT_JOB_CLAUDE_BIN": _provider_binary("AGENT_JOB_CLAUDE_BIN", "claude", (home / ".local/bin/claude",)),
        "AGENT_JOB_KIMI_BIN": _provider_binary("AGENT_JOB_KIMI_BIN", "kimi", (home / ".kimi-code/bin/kimi",)),
        "AGENT_JOB_CODEX_BIN": _provider_binary("AGENT_JOB_CODEX_BIN", "codex", (home / ".local/bin/codex", Path("/opt/homebrew/bin/codex"))),
    }
    if os.environ.get("AGENT_JOB_PROFILE_ENV"):
        environment["AGENT_JOB_PROFILE_ENV"] = os.environ["AGENT_JOB_PROFILE_ENV"]
    for name in (
        "AGENT_JOB_EXECUTION_BACKEND",
        "AGENT_JOB_CAO_URL",
        "AGENT_JOB_CAO_TOKEN",
        "AGENT_JOB_CAO_LAUNCH_TIMEOUT",
        "AGENT_JOB_CAO_PROVIDERS",
        "AGENT_JOB_CAO_CANARY_PROVIDERS",
        "AGENT_JOB_CAO_CANARY_OWNER_PREFIXES",
        "AGENT_JOB_CLAUDE_CONCURRENCY",
        "AGENT_JOB_CODEX_CONCURRENCY",
        "AGENT_JOB_KIMI_CONCURRENCY",
        "AGENT_JOB_KIMI_DEFAULT_MODEL",
        "AGENT_JOB_MAX_LOG_BYTES",
        "AGENT_JOB_MAX_EVENT_BYTES",
        "AGENT_JOB_MAX_PARTIAL_RESPONSE_BYTES",
        "AGENT_JOB_RETENTION_SECONDS",
    ):
        if os.environ.get(name):
            environment[name] = os.environ[name]
    return environment


def install() -> None:
    old_ping = _socket_request({"action": "ping"})
    active = _active_jobs() if old_ping else []
    if active:
        identifiers = ", ".join(str(job.get("job_id")) for job in active[:5])
        raise RuntimeError(
            f"Refusing to replace the supervisor while {len(active)} job(s) are active: {identifiers}"
        )
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_DIR, 0o700)
    if not IMPLEMENT_TOKEN_PATH.is_file() or not IMPLEMENT_TOKEN_PATH.read_text(encoding="utf-8").strip():
        IMPLEMENT_TOKEN_PATH.write_text(secrets.token_urlsafe(48) + "\n", encoding="utf-8")
    os.chmod(IMPLEMENT_TOKEN_PATH, 0o600)
    for name in ("supervisor.stdout.log", "supervisor.stderr.log"):
        path = STATE_DIR / name
        if path.is_file() and path.stat().st_size > 1024 * 1024:
            rotated = path.with_suffix(path.suffix + ".1")
            if rotated.exists():
                rotated.unlink()
            path.replace(rotated)
    payload = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, str(SUPERVISOR), "serve"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 5,
        "ProcessType": "Background",
        "Umask": 0o077,
        "EnvironmentVariables": _service_environment(),
        "StandardOutPath": str(STATE_DIR / "supervisor.stdout.log"),
        "StandardErrorPath": str(STATE_DIR / "supervisor.stderr.log"),
    }
    domain = f"gui/{os.getuid()}"
    _run("launchctl", "bootout", domain, str(PLIST_PATH), check=False)
    if old_ping:
        old_pid = int(old_ping.get("pid") or 0)
        for _ in range(100):
            current = _socket_request({"action": "ping"})
            if current is None or int(current.get("pid") or 0) != old_pid:
                break
            time.sleep(.1)
        else:
            raise RuntimeError(f"Previous {LABEL} process {old_pid} did not stop")
    with PLIST_PATH.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=True)
    os.chmod(PLIST_PATH, 0o600)
    _run("launchctl", "bootstrap", domain, str(PLIST_PATH))
    _run("launchctl", "kickstart", "-k", f"{domain}/{LABEL}")
    ready = None
    for _ in range(100):
        ready = _socket_request({"action": "ping"})
        if ready and (not old_ping or ready.get("pid") != old_ping.get("pid")):
            break
        time.sleep(.1)
    else:
        raise RuntimeError(f"{LABEL} did not become ready at {STATE_DIR / 'supervisor.sock'}")
    service = _run("launchctl", "print", f"{domain}/{LABEL}")
    if str(SUPERVISOR) not in service.stdout or f"pid = {ready['pid']}" not in service.stdout:
        raise RuntimeError("launchd state does not match the newly installed supervisor")
    print(f"Installed and started {LABEL} (pid {ready['pid']})")


def uninstall() -> None:
    domain = f"gui/{os.getuid()}"
    _run("launchctl", "bootout", domain, str(PLIST_PATH), check=False)
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()
    print(f"Removed {LABEL}; durable job history remains in {STATE_DIR}")


def status() -> int:
    result = _run("launchctl", "print", f"gui/{os.getuid()}/{LABEL}", check=False)
    output = result.stdout or result.stderr
    print(output.strip())
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "uninstall", "status"))
    args = parser.parse_args()
    if args.action == "install":
        install()
        return 0
    if args.action == "uninstall":
        uninstall()
        return 0
    return status()


if __name__ == "__main__":
    raise SystemExit(main())
