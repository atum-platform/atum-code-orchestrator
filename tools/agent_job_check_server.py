#!/usr/bin/env python3
"""MCP broker for caller-approved, sandboxed implementation checks."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
from typing import Any

from mcp.server.fastmcp import FastMCP


MAX_CAPTURE_BYTES = 256 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_TIMEOUT_SECONDS = 900
CHECK_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
PROFILE = """(version 1)
(allow default)
(deny network*)
(deny appleevent-send)
(deny process-exec (literal "/bin/launchctl"))
(deny process-exec (literal "/usr/bin/osascript"))
(deny file-write*
  (require-all
    (require-not (subpath (param "WORKDIR")))
    (require-not (subpath (param "RUNTIME_DIR")))
    (require-not (literal "/dev/null"))))
(deny file-write* (subpath (param "GIT_META")))
(deny file-read* (subpath (param "SSH_DIR")))
(deny file-read* (subpath (param "AWS_DIR")))
(deny file-read* (subpath (param "KUBE_DIR")))
(deny file-read* (subpath (param "GH_DIR")))
(deny file-read* (subpath (param "CLAUDE_DIR")))
(deny file-read* (subpath (param "CODEX_DIR")))
(deny file-read* (subpath (param "KIMI_DIR")))
(deny file-read* (literal (param "CLAUDE_CONFIG")))
(deny file-read* (literal (param "NETRC")))
(deny file-read* (literal (param "NPMRC")))
(deny file-read* (subpath (param "KEYCHAINS_DIR")))
"""


def _load_contract() -> tuple[Path, Path, dict[str, dict[str, Any]]]:
    workdir = Path(os.environ["ACO_CHECKS_WORKDIR"]).resolve()
    runtime = Path(os.environ["ACO_CHECKS_RUNTIME"]).resolve()
    checks = json.loads(os.environ.get("ACO_CHECKS_JSON", "[]"))
    if not workdir.is_dir() or not runtime.is_dir():
        raise RuntimeError("Approved-check workspace or runtime is unavailable")
    return workdir, runtime, {str(check["name"]): check for check in checks}


WORKDIR, RUNTIME, CHECKS = _load_contract()
RUN_LOCK = threading.Lock()
PROCESS_LOCK = threading.Lock()
ACTIVE_PROCESS: subprocess.Popen[bytes] | None = None
mcp = FastMCP("aco-checks")


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        except PermissionError:
            try:
                process.send_signal(sig)
            except ProcessLookupError:
                return
        if sig == signal.SIGTERM:
            time.sleep(0.1)


def _command_env() -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items()
        if key in {"HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "LC_CTYPE"}
    }
    env.update({"CI": "1", "TMPDIR": f"{RUNTIME}{os.sep}"})
    return env


def _process_start(pid: int) -> str:
    result = subprocess.run(
        ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
        check=False, capture_output=True, text=True, timeout=2,
    )
    return result.stdout.strip()


def _set_active_process(process: subprocess.Popen[bytes] | None) -> None:
    global ACTIVE_PROCESS
    with PROCESS_LOCK:
        ACTIVE_PROCESS = process


def _stop_active_process() -> None:
    with PROCESS_LOCK:
        process = ACTIVE_PROCESS
    if process is not None:
        _kill_group(process)


def _termination_handler(_signum: int, _frame: Any) -> None:
    _stop_active_process()
    raise SystemExit(128)


def _watch_parent(parent_pid: int) -> None:
    while True:
        time.sleep(0.2)
        if os.getppid() != parent_pid:
            _stop_active_process()
            return


def _run(name: str) -> dict[str, Any]:
    check = CHECKS.get(name)
    if check is None or not CHECK_NAME_PATTERN.fullmatch(name):
        return {"ok": False, "error": "Unknown check name", "available": sorted(CHECKS)}
    stdout_path = RUNTIME / f"check-{name}.stdout"
    stderr_path = RUNTIME / f"check-{name}.stderr"
    argv = [
        str(SANDBOX_EXEC), "-p", PROFILE,
        "-D", f"WORKDIR={WORKDIR}",
        "-D", f"RUNTIME_DIR={RUNTIME}",
        "-D", f"GIT_META={WORKDIR / '.git'}",
        "-D", f"SSH_DIR={Path.home() / '.ssh'}",
        "-D", f"AWS_DIR={Path.home() / '.aws'}",
        "-D", f"KUBE_DIR={Path.home() / '.kube'}",
        "-D", f"GH_DIR={Path.home() / '.config' / 'gh'}",
        "-D", f"CLAUDE_DIR={Path.home() / '.claude'}",
        "-D", f"CODEX_DIR={Path.home() / '.codex'}",
        "-D", f"KIMI_DIR={Path.home() / '.kimi'}",
        "-D", f"CLAUDE_CONFIG={Path.home() / '.claude.json'}",
        "-D", f"NETRC={Path.home() / '.netrc'}",
        "-D", f"NPMRC={Path.home() / '.npmrc'}",
        "-D", f"KEYCHAINS_DIR={Path.home() / 'Library' / 'Keychains'}",
        *check["argv"],
    ]
    timeout = max(5, min(int(check["timeout_seconds"]), MAX_TIMEOUT_SECONDS))
    started = time.monotonic()
    process: subprocess.Popen[bytes] | None = None
    exceeded = False
    timed_out = False
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        try:
            process = subprocess.Popen(
                argv, cwd=WORKDIR, env=_command_env(), stdout=stdout, stderr=stderr,
                start_new_session=True,
            )
            _set_active_process(process)
            process_record = RUNTIME / f"check-{name}.process.json"
            process_record.write_text(json.dumps({
                "pid": process.pid,
                "process_start": _process_start(process.pid),
            }), encoding="utf-8")
            while process.poll() is None:
                if time.monotonic() - started > timeout:
                    timed_out = True
                    break
                if stdout_path.stat().st_size + stderr_path.stat().st_size > MAX_CAPTURE_BYTES:
                    exceeded = True
                    break
                time.sleep(0.05)
        except OSError as exc:
            return {"ok": False, "error": f"Check could not start: {exc}"}
        finally:
            if process is not None:
                _kill_group(process)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                _set_active_process(None)
                (RUNTIME / f"check-{name}.process.json").unlink(missing_ok=True)
    stdout_text = stdout_path.read_bytes()[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    stderr_text = stderr_path.read_bytes()[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    return {
        "ok": bool(process and process.returncode == 0 and not timed_out and not exceeded),
        "name": name,
        "exit_code": process.returncode if process else None,
        "timed_out": timed_out,
        "output_limit_exceeded": exceeded,
        "duration_seconds": round(time.monotonic() - started, 3),
        "stdout": stdout_text,
        "stderr": stderr_text,
        "output_truncated": (
            stdout_path.stat().st_size > MAX_OUTPUT_BYTES
            or stderr_path.stat().st_size > MAX_OUTPUT_BYTES
        ),
    }


@mcp.tool()
async def run_check(name: str) -> str:
    """Run one caller-approved named verification check; no arbitrary command input is accepted."""
    def run_serialized() -> dict[str, Any]:
        with RUN_LOCK:
            return _run(name)

    return json.dumps(await asyncio.to_thread(run_serialized), ensure_ascii=False)


if __name__ == "__main__":
    if not SANDBOX_EXEC.is_file():
        raise SystemExit("Approved checks require macOS sandbox-exec")
    signal.signal(signal.SIGTERM, _termination_handler)
    signal.signal(signal.SIGINT, _termination_handler)
    threading.Thread(target=_watch_parent, args=(os.getppid(),), daemon=True).start()
    mcp.run(transport="stdio")
