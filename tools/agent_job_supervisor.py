#!/usr/bin/env python3
"""Durable, machine-wide supervisor for local AI CLI jobs."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import stat
import sys
import time
import uuid
from typing import Any, Callable

from agent_job_events import (
    MAX_EVENT_RECORD_BYTES,
    MAX_EVENT_TEXT_CHARS,
    ProviderEventDecoder,
    bound_event_payload,
)
from agent_job_policy import configured_allowed_roots, SENSITIVE_PATH_PARTS


STATE_DIR = Path(os.environ.get("AGENT_JOB_STATE_DIR", "~/.local/state/agent-job-supervisor")).expanduser()
SOCKET_PATH = Path(os.environ.get("AGENT_JOB_SOCKET", str(STATE_DIR / "supervisor.sock"))).expanduser()
DB_PATH = Path(os.environ.get("AGENT_JOB_DB", str(STATE_DIR / "jobs.sqlite3"))).expanduser()
LOG_DIR = Path(os.environ.get("AGENT_JOB_LOG_DIR", str(STATE_DIR / "logs"))).expanduser()
SERVER_DIR = Path(__file__).resolve().parent
MAX_PROMPT_BYTES = 4 * 1024 * 1024
MAX_READ_BYTES = 256_000
MAX_JOB_LOG_BYTES = int(os.environ.get("AGENT_JOB_MAX_LOG_BYTES", str(10 * 1024 * 1024)))
MAX_EVENT_LOG_BYTES = int(os.environ.get("AGENT_JOB_MAX_EVENT_BYTES", str(2 * 1024 * 1024)))
MAX_PARTIAL_RESPONSE_BYTES = int(
    os.environ.get("AGENT_JOB_MAX_PARTIAL_RESPONSE_BYTES", str(256 * 1024))
)
JOB_RETENTION_SECONDS = int(os.environ.get("AGENT_JOB_RETENTION_SECONDS", str(14 * 24 * 3600)))
MIN_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 7200
DEFAULT_SOFT_STALL_SECONDS = int(os.environ.get("AGENT_JOB_SOFT_STALL_SECONDS", "300"))
IMPLEMENT_TOKEN_PATH = Path(
    os.environ.get("AGENT_JOB_IMPLEMENT_TOKEN_FILE", str(STATE_DIR / "implement.token"))
).expanduser()
MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
KIMI_DEFAULT_MODEL = "kimi-code/k3"
KIMI_K27_MODEL = "kimi-code/kimi-for-coding"
KIMI_MODEL_ALIASES = {
    "k3": KIMI_DEFAULT_MODEL,
    "kimi-k3": KIMI_DEFAULT_MODEL,
    "kimi-code/k3": KIMI_DEFAULT_MODEL,
    "k3-1m": KIMI_DEFAULT_MODEL,
    "kimi-k3-1m": KIMI_DEFAULT_MODEL,
    "kimi-code/k3-1m": KIMI_DEFAULT_MODEL,
    "k3-256k": "kimi-code/k3-256k",
    "kimi-k3-256k": "kimi-code/k3-256k",
    "kimi-code/k3-256k": "kimi-code/k3-256k",
    "k2.7": KIMI_K27_MODEL,
    "kimi-k2.7": KIMI_K27_MODEL,
    "kimi-for-coding": KIMI_K27_MODEL,
    "kimi-code/kimi-for-coding": KIMI_K27_MODEL,
    "kimi-for-coding-highspeed": "kimi-code/kimi-for-coding-highspeed",
    "kimi-code/kimi-for-coding-highspeed": "kimi-code/kimi-for-coding-highspeed",
}
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
SEMANTIC_PROGRESS_KINDS = {
    "turn_started", "thinking_delta", "message_delta", "tool_started",
    "tool_finished", "progress", "usage", "provider_raw", "parse_error",
    "warning", "waiting", "job_started",
}
SAFE_ENV_KEYS = {
    "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "__CF_USER_TEXT_ENCODING",
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
}
CAO_ENV_KEYS = {"AGENT_JOB_CAO_URL", "AGENT_JOB_CAO_TOKEN", "AGENT_JOB_CAO_LAUNCH_TIMEOUT"}
CLAUDE_AUTH_KEYS = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"}
KIMI_AUTH_KEYS = {"KIMI_API_KEY", "KIMI_CN_API_KEY", "MOONSHOT_API_KEY", "MOONSHOT_API_BASE"}
PROVIDER_AUTH_KEYS = {"claude": CLAUDE_AUTH_KEYS, "kimi": KIMI_AUTH_KEYS, "codex": set()}
SEMANTIC_PROVIDERS = {"claude", "codex", "kimi"}
SEMANTIC_LIVENESS_PROVIDERS = {"claude", "codex"}


class AlreadyRunning(RuntimeError):
    pass


def _now() -> float:
    return time.time()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _kimi_semantic_enabled() -> bool:
    return os.environ.get("AGENT_JOB_KIMI_SEMANTIC", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _normalize_model(provider: str, requested_model: str) -> tuple[str, str]:
    model = requested_model.strip()
    if provider != "kimi":
        return model, ""
    if not model or model.lower() in {"auto", "default", "kimi"}:
        return os.environ.get("AGENT_JOB_KIMI_DEFAULT_MODEL", KIMI_DEFAULT_MODEL), ""
    normalized = KIMI_MODEL_ALIASES.get(model.lower())
    if normalized:
        return normalized, ""
    return KIMI_K27_MODEL, (
        f"Unrecognized or legacy Kimi model alias '{model}' normalized to {KIMI_K27_MODEL}"
    )


def _allowed_roots() -> list[Path]:
    return configured_allowed_roots()


def _safe_workdir(value: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ValueError("Workdir must be an absolute path")
    path = candidate.resolve()
    if not path.is_dir():
        raise ValueError(f"Workdir does not exist or is not a directory: {path}")
    roots = [root for root in _allowed_roots() if root.exists()]
    if not roots:
        raise ValueError("No configured agent-job workspace roots exist; refusing to run fail-open")
    if not any(path == root or root in path.parents for root in roots):
        raise ValueError(f"Workdir is outside configured roots: {path}")
    if any(part.lower() in SENSITIVE_PATH_PARTS for part in path.parts):
        raise ValueError(f"Workdir is inside a credential or secret store: {path}")
    return path


def _find_binary(provider: str) -> str:
    env_name = f"AGENT_JOB_{provider.upper()}_BIN"
    known = {
        "claude": ["~/.local/bin/claude", "/opt/homebrew/bin/claude"],
        "kimi": ["~/.kimi-code/bin/kimi", "/opt/homebrew/bin/kimi"],
        "codex": ["/opt/homebrew/bin/codex", "~/.local/bin/codex"],
    }
    candidates = [os.environ.get(env_name, ""), shutil.which(provider)] + known[provider]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser().resolve()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise RuntimeError(f"{provider} CLI not found; set {env_name}")


def _provider_env(provider: str) -> dict[str, str]:
    inherited = os.environ
    env = {key: inherited[key] for key in SAFE_ENV_KEYS if inherited.get(key)}
    env.setdefault("HOME", str(Path.home()))
    env.setdefault("USER", Path.home().name)
    env.setdefault("LOGNAME", env["USER"])
    env.setdefault("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
    profile: dict[str, str] = {}
    profile_path = os.environ.get("AGENT_JOB_PROFILE_ENV", "").strip()
    if profile_path and Path(profile_path).expanduser().is_file():
        for raw in Path(profile_path).expanduser().read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, value = line.split("=", 1)
            profile[key.strip()] = value.strip().strip('"').strip("'")
    for key in PROVIDER_AUTH_KEYS[provider]:
        value = profile.get(key) or inherited.get(key)
        if value:
            env[key] = value
    for key in (CLAUDE_AUTH_KEYS | KIMI_AUTH_KEYS) - PROVIDER_AUTH_KEYS[provider]:
        env.pop(key, None)
    env["AGENT_JOB_DEPTH"] = str(int(inherited.get("AGENT_JOB_DEPTH", "0") or 0) + 1)
    env["AGENT_JOB_PROVIDER"] = provider
    if provider == "kimi":
        env["KIMI_CODE_EXPERIMENTAL_FLAG"] = "1"
    return env


def _cao_bridge_env(provider: str) -> dict[str, str]:
    """Build the bridge environment without forwarding provider credentials."""
    env = _provider_env(provider)
    for key in CLAUDE_AUTH_KEYS | KIMI_AUTH_KEYS:
        env.pop(key, None)
    env.pop("KIMI_CODE_EXPERIMENTAL_FLAG", None)
    for key in CAO_ENV_KEYS:
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def _csv_values(name: str) -> set[str]:
    return {item.strip() for item in os.environ.get(name, "").split(",") if item.strip()}


def _execution_backend(provider: str, owner: str) -> str:
    default = os.environ.get("AGENT_JOB_EXECUTION_BACKEND", "native")
    if default not in {"native", "cao"}:
        raise ValueError(f"Unsupported execution backend: {default}")
    if provider in _csv_values("AGENT_JOB_CAO_PROVIDERS"):
        return "cao"
    canary_providers = _csv_values("AGENT_JOB_CAO_CANARY_PROVIDERS")
    canary_prefixes = _csv_values("AGENT_JOB_CAO_CANARY_OWNER_PREFIXES")
    if provider in canary_providers and any(owner.startswith(prefix) for prefix in canary_prefixes):
        return "cao"
    return default


class JobStore:
    def __init__(self, path: Path, on_change: Callable[[str], None] | None = None):
        self.path = path
        self.on_change = on_change
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                mode TEXT NOT NULL,
                workdir TEXT NOT NULL,
                prompt TEXT NOT NULL,
                owner TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                failure_kind TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                started_at REAL,
                updated_at REAL NOT NULL,
                last_output_at REAL,
                finished_at REAL,
                timeout_seconds INTEGER NOT NULL,
                soft_stall_seconds INTEGER NOT NULL,
                max_turns INTEGER NOT NULL,
                pid INTEGER,
                pgid INTEGER,
                exit_code INTEGER,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                log_path TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                delivery_id TEXT PRIMARY KEY,
                job_id TEXT UNIQUE NOT NULL,
                owner TEXT NOT NULL,
                created_at REAL NOT NULL,
                acked_at REAL
            );
            CREATE TABLE IF NOT EXISTS route_decisions (
                decision_id TEXT PRIMARY KEY,
                protocol_version INTEGER NOT NULL,
                policy_version TEXT NOT NULL,
                mode TEXT NOT NULL,
                caller_provider TEXT NOT NULL,
                surface TEXT NOT NULL,
                capability TEXT NOT NULL,
                lane TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT '',
                model_alias TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                expires_at REAL,
                owner TEXT NOT NULL DEFAULT '',
                request_json TEXT NOT NULL,
                response_json TEXT NOT NULL
            );
            """
        )
        columns = {str(row["name"]) for row in self.db.execute("PRAGMA table_info(jobs)")}
        if "idempotency_key" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT ''")
        if "request_hash" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN request_hash TEXT NOT NULL DEFAULT ''")
        if "binary_path" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN binary_path TEXT NOT NULL DEFAULT ''")
        if "process_start" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN process_start TEXT NOT NULL DEFAULT ''")
        if "execution_backend" not in columns:
            self.db.execute(
                "ALTER TABLE jobs ADD COLUMN execution_backend TEXT NOT NULL DEFAULT 'native'"
            )
        if "semantic_stream" not in columns:
            self.db.execute(
                "ALTER TABLE jobs ADD COLUMN semantic_stream INTEGER NOT NULL DEFAULT 0"
            )
            self.db.execute(
                """UPDATE jobs SET semantic_stream = 1
                   WHERE provider IN ('claude', 'codex') AND execution_backend = 'native'"""
            )
        migrations = {
            "requested_model": "TEXT NOT NULL DEFAULT ''",
            "last_event_at": "REAL",
            "last_event_kind": "TEXT NOT NULL DEFAULT ''",
            "last_progress_at": "REAL",
            "open_tool": "TEXT NOT NULL DEFAULT ''",
            "open_tool_since": "REAL",
            "open_tool_count": "INTEGER NOT NULL DEFAULT 0",
            "partial_response_bytes": "INTEGER NOT NULL DEFAULT 0",
            "partial_response_truncated": "INTEGER NOT NULL DEFAULT 0",
            "provider_result_error": "INTEGER NOT NULL DEFAULT 0",
            "semantic_normalization_failed": "INTEGER NOT NULL DEFAULT 0",
            "journal_truncated": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in migrations.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
        self.db.execute("CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created_at)")
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotency ON jobs(idempotency_key) WHERE idempotency_key <> ''"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS route_decisions_created ON route_decisions(created_at)"
        )
        self.db.execute(
            "UPDATE jobs SET prompt = '' WHERE status IN ('completed','failed','cancelled','interrupted')"
        )
        self.db.commit()
        try:
            os.chmod(path, 0o600)
        except FileNotFoundError:
            pass

    def create(self, spec: dict[str, Any], job_id: str, log_path: Path) -> dict[str, Any]:
        key = str(spec.get("idempotency_key") or "")
        if key:
            existing = self.db.execute("SELECT * FROM jobs WHERE idempotency_key = ?", (key,)).fetchone()
            if existing:
                job = dict(existing)
                if job.get("request_hash") != spec["request_hash"]:
                    raise ValueError("Idempotency key was already used for a different job specification")
                return job
        now = _now()
        self.db.execute(
            """INSERT INTO jobs (
                job_id, provider, model, requested_model, mode, workdir, prompt, owner,
                status, message,
                created_at, updated_at, timeout_seconds, soft_stall_seconds,
                max_turns, log_path, idempotency_key, request_hash, execution_backend,
                semantic_stream
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id, spec["provider"], spec["model"], spec.get("requested_model", ""),
                spec["mode"], spec["workdir"],
                spec["prompt"], spec.get("owner", ""), spec.get("message", ""), now, now,
                spec["timeout_seconds"],
                spec["soft_stall_seconds"], spec["max_turns"], str(log_path), key,
                spec["request_hash"], spec.get("execution_backend", "native"),
                int(spec.get("semantic_stream") or 0),
            ),
        )
        self.db.commit()
        if self.on_change is not None:
            self.on_change(job_id)
        return self.get(job_id)

    def create_route_decision(
        self, intent: dict[str, Any], decision: dict[str, Any], owner: str
    ) -> dict[str, Any]:
        decision_id = str(uuid.uuid4())
        created_at = _now()
        response = {"decision_id": decision_id, "created_at": created_at, **decision}
        self.db.execute(
            """INSERT INTO route_decisions (
                decision_id, protocol_version, policy_version, mode, caller_provider,
                surface, capability, lane, provider, model_alias, created_at,
                expires_at, owner, request_json, response_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                decision_id, response["protocol_version"], response["policy_version"],
                response["mode"], intent["caller_provider"], intent["surface"],
                intent["capability"], response["lane"], response["provider"],
                response["model_alias"], created_at, response["expires_at"], owner,
                _json(intent), _json(response),
            ),
        )
        self.db.commit()
        return response

    def get(self, job_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if not row:
            raise ValueError(f"Unknown job id: {job_id}")
        return dict(row)

    def update(self, job_id: str, *, notify: bool = True, **values: Any) -> dict[str, Any]:
        if not values:
            return self.get(job_id)
        values["updated_at"] = _now()
        columns = ", ".join(f"{name} = ?" for name in values)
        self.db.execute(f"UPDATE jobs SET {columns} WHERE job_id = ?", (*values.values(), job_id))
        if values.get("status") in TERMINAL_STATUSES:
            self.db.execute(
                """INSERT OR IGNORE INTO deliveries (delivery_id, job_id, owner, created_at)
                   SELECT job_id, job_id, owner, COALESCE(finished_at, updated_at)
                   FROM jobs WHERE job_id = ? AND owner <> ''""",
                (job_id,),
            )
        self.db.commit()
        if notify and self.on_change is not None:
            self.on_change(job_id)
        return self.get(job_id)

    def touch(self, job_id: str) -> None:
        self.db.execute("UPDATE jobs SET updated_at = ? WHERE job_id = ?", (_now(), job_id))
        self.db.commit()

    def queued(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at"
        )]

    def claim(self, job_id: str) -> bool:
        cursor = self.db.execute(
            "UPDATE jobs SET status = 'launching', updated_at = ? WHERE job_id = ? AND status = 'queued'",
            (_now(), job_id),
        )
        self.db.commit()
        if cursor.rowcount == 1 and self.on_change is not None:
            self.on_change(job_id)
        return cursor.rowcount == 1

    def running(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM jobs WHERE status IN ('launching','running') ORDER BY created_at"
        )]

    def list(self, status: str = "", limit: int = 50, owner: str = "") -> list[dict[str, Any]]:
        if status == "possibly_stalled" and owner:
            rows = self.db.execute(
                """SELECT * FROM jobs WHERE status = 'running' AND instr(owner, ?) = 1
                   ORDER BY created_at DESC LIMIT ?""",
                (owner, max(limit * 10, 200)),
            )
        elif status == "possibly_stalled":
            rows = self.db.execute(
                """SELECT * FROM jobs WHERE status = 'running'
                   ORDER BY created_at DESC LIMIT ?""",
                (max(limit * 10, 200),),
            )
        elif status and owner:
            rows = self.db.execute(
                "SELECT * FROM jobs WHERE status = ? AND instr(owner, ?) = 1 ORDER BY created_at DESC LIMIT ?",
                (status, owner, limit),
            )
        elif status:
            rows = self.db.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?", (status, limit)
            )
        elif owner:
            rows = self.db.execute(
                "SELECT * FROM jobs WHERE instr(owner, ?) = 1 ORDER BY created_at DESC LIMIT ?", (owner, limit)
            )
        else:
            rows = self.db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(row) for row in rows]

    def reconcile(self) -> list[dict[str, Any]]:
        interrupted = self.running()
        now = _now()
        self.db.execute(
            """UPDATE jobs SET status = 'interrupted', failure_kind = 'supervisor_restart',
               message = 'Supervisor restarted while the job was running', prompt = '', finished_at = ?, updated_at = ?
               WHERE status IN ('launching','running')""",
            (now, now),
        )
        self.db.execute(
            """INSERT OR IGNORE INTO deliveries (delivery_id, job_id, owner, created_at)
               SELECT job_id, job_id, owner, ? FROM jobs
               WHERE status = 'interrupted' AND owner <> '' AND finished_at = ?""",
            (now, now),
        )
        self.db.commit()
        return interrupted

    def inbox(
        self, owner: str, limit: int = 20, ack_delivery_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        acknowledgements = list(dict.fromkeys(ack_delivery_ids or []))[:100]
        if acknowledgements:
            placeholders = ",".join("?" for _ in acknowledgements)
            self.db.execute(
                f"UPDATE deliveries SET acked_at = COALESCE(acked_at, ?) "
                f"WHERE owner = ? AND delivery_id IN ({placeholders})",
                (_now(), owner, *acknowledgements),
            )
        rows = self.db.execute(
            """SELECT d.delivery_id, d.created_at AS delivery_created_at, j.*
               FROM deliveries d JOIN jobs j ON j.job_id = d.job_id
               WHERE d.owner = ? AND d.acked_at IS NULL
               ORDER BY d.created_at ASC LIMIT ?""",
            (owner, limit),
        )
        self.db.commit()
        return [dict(row) for row in rows]

    def prune(self, cutoff: float) -> list[str]:
        rows = self.db.execute(
            "SELECT log_path FROM jobs WHERE status IN ('completed','failed','cancelled','interrupted') AND finished_at < ?",
            (cutoff,),
        ).fetchall()
        self.db.execute(
            "DELETE FROM deliveries WHERE job_id IN "
            "(SELECT job_id FROM jobs WHERE status IN ('completed','failed','cancelled','interrupted') "
            "AND finished_at < ?)",
            (cutoff,),
        )
        self.db.execute(
            "DELETE FROM jobs WHERE status IN ('completed','failed','cancelled','interrupted') AND finished_at < ?",
            (cutoff,),
        )
        self.db.execute("DELETE FROM route_decisions WHERE created_at < ?", (cutoff,))
        self.db.commit()
        return [str(row["log_path"]) for row in rows]


CommandBuilder = Callable[[dict[str, Any]], tuple[list[str], str | None, dict[str, str]]]


class Supervisor:
    def __init__(
        self,
        state_dir: Path = STATE_DIR,
        socket_path: Path = SOCKET_PATH,
        db_path: Path = DB_PATH,
        log_dir: Path = LOG_DIR,
        command_builder: CommandBuilder | None = None,
        binary_finder: Callable[[str], str] | None = None,
    ):
        self.state_dir = state_dir
        self.socket_path = socket_path
        self.log_dir = log_dir
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_dir, 0o700)
        os.chmod(self.log_dir, 0o700)
        self.change_events: dict[str, asyncio.Event] = {}
        self.store = JobStore(db_path, self._signal_change)
        self.binary_finder = binary_finder or _find_binary
        self.command_builder = command_builder or self._build_command
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.log_locks: dict[str, asyncio.Lock] = {}
        self.last_output_writes: dict[str, float] = {}
        self.last_event_writes: dict[str, float] = {}
        self.job_log_paths: dict[str, Path] = {}
        self.event_decoders: dict[str, ProviderEventDecoder] = {}
        self.event_sequences: dict[str, int] = {}
        self.event_summaries: dict[str, dict[str, Any]] = {}
        self.event_truncated: set[str] = set()
        self.normalization_failed: set[str] = set()
        self.open_tools: dict[str, dict[str, tuple[str, float]]] = {}
        self.provider_limits = {
            "claude": int(os.environ.get("AGENT_JOB_CLAUDE_CONCURRENCY", "2")),
            "kimi": int(os.environ.get("AGENT_JOB_KIMI_CONCURRENCY", "1")),
            "codex": int(os.environ.get("AGENT_JOB_CODEX_CONCURRENCY", "2")),
        }
        self._stopping = False
        self._lock_handle = None

    def _signal_change(self, job_id: str) -> None:
        event = self.change_events.get(job_id)
        if event is not None:
            event.set()

    def _build_command(self, job: dict[str, Any]) -> tuple[list[str], str | None, dict[str, str]]:
        provider = job["provider"]
        if job.get("execution_backend", "native") == "cao":
            bridge = SERVER_DIR / "cao_job_bridge.py"
            if not bridge.is_file():
                raise RuntimeError(f"CAO job bridge is missing: {bridge}")
            argv = [
                sys.executable,
                str(bridge),
                "--provider",
                provider,
                "--model",
                job["model"],
                "--mode",
                job["mode"],
                "--workdir",
                job["workdir"],
                "--job-id",
                job["job_id"],
            ]
            env = _cao_bridge_env(provider)
            env["AGENT_JOB_DEADLINE_EPOCH"] = str(
                float(job["created_at"]) + int(job["timeout_seconds"])
            )
            return argv, job["prompt"], env
        binary = self.binary_finder(provider)
        model = job["model"]
        mode = job["mode"]
        prompt = job["prompt"]
        max_turns = int(job["max_turns"])
        if provider == "claude":
            permission = "plan" if mode == "readonly" else "acceptEdits"
            tools = ["Read", "Glob", "Grep", "LS"] if mode == "readonly" else ["Read", "Glob", "Grep", "Edit", "Write"]
            argv = [
                binary, "-p", "--model", model, "--permission-mode", permission,
                "--allowed-tools", *tools,
                "--output-format", "stream-json", "--include-partial-messages", "--verbose",
                "--no-session-persistence",
            ]
            if max_turns > 0:
                argv.extend(["--max-turns", str(max_turns)])
            argv.extend(["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'])
            if mode == "readonly":
                argv.append("--safe-mode")
            return argv, prompt, _provider_env(provider)
        if provider == "kimi":
            agent_name = "kimi_read_only_reviewer.md" if mode == "readonly" else "kimi_implementation_agent.md"
            agent_path = SERVER_DIR / agent_name
            if not agent_path.is_file():
                raise RuntimeError(f"Kimi agent definition is missing: {agent_path}")
            argv = [binary, "--model", model, "--agent-file", str(agent_path)]
            if job.get("semantic_stream"):
                argv.extend(["--output-format", "stream-json"])
            argv.extend(["--prompt", prompt])
            return argv, None, _provider_env(provider)
        sandbox = "read-only" if mode == "readonly" else "workspace-write"
        argv = [
            binary, "exec", "--ignore-user-config", "-C", job["workdir"],
            "-s", sandbox, "--json", "--skip-git-repo-check",
        ]
        if model:
            argv.extend(["--model", model])
        argv.append("-")
        return argv, prompt, _provider_env(provider)

    def _public(self, job: dict[str, Any]) -> dict[str, Any]:
        result = {key: value for key, value in job.items() if key != "prompt"}
        summary = self.event_summaries.get(str(job["job_id"]))
        if summary:
            result.update(summary)
        lifecycle_status = str(result["status"])
        result["lifecycle_status"] = lifecycle_status
        if lifecycle_status in {"queued", "launching"}:
            result["activity"] = "starting"
        elif lifecycle_status in TERMINAL_STATUSES:
            result["activity"] = "terminal"
        elif lifecycle_status == "running":
            now = _now()
            output_anchor = result.get("last_output_at") or result.get("started_at") or result["created_at"]
            output_silence = max(0, int(now - float(output_anchor)))
            result["seconds_without_output"] = output_silence
            semantic_stream = self._semantic_liveness_active(result)
            progress_anchor = result.get("last_progress_at") if semantic_stream else output_anchor
            if progress_anchor is None:
                progress_anchor = result.get("started_at") if semantic_stream else output_anchor
            progress_silence = max(0, int(now - float(progress_anchor)))
            result["seconds_without_progress"] = progress_silence
            open_tool = str(result.get("open_tool") or "")
            if open_tool:
                result["activity"] = f"tool_running:{open_tool}"
                if result.get("open_tool_since"):
                    result["open_tool_seconds"] = max(
                        0, int(now - float(result["open_tool_since"]))
                    )
            elif result.get("last_event_kind") == "waiting":
                if progress_silence < int(result["soft_stall_seconds"]):
                    result["activity"] = "waiting_on_provider"
                else:
                    result["activity"] = "idle_unknown"
                    result["status"] = "possibly_stalled"
            elif progress_silence >= int(result["soft_stall_seconds"]):
                result["activity"] = "idle_unknown"
                # Compatibility alias for existing callers during the transition.
                result["status"] = "possibly_stalled"
            elif result.get("last_event_kind") == "message_delta":
                result["activity"] = "streaming"
            elif result.get("last_event_kind") in {"turn_started", "thinking_delta"}:
                result["activity"] = "reasoning"
            else:
                result["activity"] = "waiting_on_provider"
        result["has_partial_response"] = bool(result.get("partial_response_bytes"))
        partial_path = self._partial_path(result)
        if partial_path.is_file():
            result["partial_response_bytes"] = max(
                int(result.get("partial_response_bytes") or 0), partial_path.stat().st_size
            )
            result["has_partial_response"] = True
        return result

    @staticmethod
    def _has_semantic_adapter(job: dict[str, Any]) -> bool:
        return (
            bool(job.get("semantic_stream"))
            and job.get("provider") in SEMANTIC_PROVIDERS
            and job.get("execution_backend", "native") == "native"
        )

    def _semantic_adapter_active(self, job: dict[str, Any]) -> bool:
        return (
            self._has_semantic_adapter(job)
            and not job.get("semantic_normalization_failed")
            and str(job["job_id"]) not in self.normalization_failed
        )

    def _semantic_liveness_active(self, job: dict[str, Any]) -> bool:
        return (
            job.get("provider") in SEMANTIC_LIVENESS_PROVIDERS
            and self._semantic_adapter_active(job)
        )

    def _private_semantic_stdout(self, job: dict[str, Any]) -> bool:
        return (
            job.get("provider") in {"claude", "kimi"}
            and self._semantic_adapter_active(job)
        )

    def _event_path(self, job: dict[str, Any]) -> Path:
        return Path(f"{job['log_path']}.events.jsonl")

    def _partial_path(self, job: dict[str, Any]) -> Path:
        return Path(f"{job['log_path']}.partial.txt")

    def _next_event_seq(self, job_id: str, path: Path) -> int:
        if job_id not in self.event_sequences:
            last = 0
            if path.is_file():
                with path.open("rb") as handle:
                    for line in handle:
                        try:
                            last = max(last, int(json.loads(line).get("seq") or 0))
                        except (ValueError, TypeError, json.JSONDecodeError):
                            continue
            self.event_sequences[job_id] = last
        self.event_sequences[job_id] += 1
        return self.event_sequences[job_id]

    def _split_event_payloads(self, kind: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        text = payload.get("text")
        if kind not in {"message_delta", "thinking_delta"} or not isinstance(text, str):
            return [payload]
        if not text:
            return [payload]
        remaining = text.encode("utf-8")
        chunks: list[str] = []
        while remaining:
            chunk = remaining[:MAX_EVENT_TEXT_CHARS].decode("utf-8", errors="ignore")
            consumed = len(chunk.encode("utf-8"))
            if consumed == 0:
                chunk = remaining.decode("utf-8", errors="replace")[0]
                consumed = len(chunk.encode("utf-8"))
            chunks.append(chunk)
            remaining = remaining[consumed:]
        return [{**payload, "text": chunk} for chunk in chunks]

    @staticmethod
    def _append_private(path: Path, data: bytes) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "ab") as handle:
            handle.write(data)

    def _write_partial_response(self, job: dict[str, Any], text: str) -> tuple[int, bool]:
        current_truncated = bool(job.get("partial_response_truncated"))
        if not text:
            return int(job.get("partial_response_bytes") or 0), current_truncated
        path = self._partial_path(job)
        current = path.stat().st_size if path.is_file() else 0
        remaining = max(0, MAX_PARTIAL_RESPONSE_BYTES - current)
        if not remaining:
            return current, True
        data = text.encode("utf-8")
        truncated = current_truncated or len(data) > remaining
        if len(data) > remaining:
            data = data[:remaining].decode("utf-8", errors="ignore").encode("utf-8")
        self._append_private(path, data)
        return current + len(data), truncated

    def _persist_event_summary(self, job_id: str, *, force: bool = False) -> None:
        summary = self.event_summaries.get(job_id)
        if not summary:
            return
        now = _now()
        if not force and now - self.last_event_writes.get(job_id, 0) < 1:
            return
        self.store.update(job_id, **summary)
        self.last_event_writes[job_id] = now

    def _record_event(
        self, job_id: str, kind: str, payload: dict[str, Any] | None = None, *, force: bool = False
    ) -> None:
        job = self.store.get(job_id)
        path = self._event_path(job)
        summary = self.event_summaries.setdefault(job_id, {
            "last_event_at": job.get("last_event_at"),
            "last_event_kind": str(job.get("last_event_kind") or ""),
            "last_progress_at": job.get("last_progress_at"),
            "open_tool": str(job.get("open_tool") or ""),
            "open_tool_since": job.get("open_tool_since"),
            "open_tool_count": int(job.get("open_tool_count") or 0),
            "partial_response_bytes": int(job.get("partial_response_bytes") or 0),
            "partial_response_truncated": int(job.get("partial_response_truncated") or 0),
            "provider_result_error": int(job.get("provider_result_error") or 0),
            "journal_truncated": int(job.get("journal_truncated") or 0),
        })
        for raw_event_payload in self._split_event_payloads(kind, payload or {}):
            if kind == "message_delta":
                partial_bytes, partial_truncated = self._write_partial_response(
                    {**job, **summary}, str(raw_event_payload.get("text") or "")
                )
                summary["partial_response_bytes"] = partial_bytes
                summary["partial_response_truncated"] = int(partial_truncated)
            event_payload = bound_event_payload(raw_event_payload)
            now = _now()
            event = {
                "v": 1,
                "seq": self._next_event_seq(job_id, path),
                "job_id": job_id,
                "ts": now,
                "provider": job["provider"],
                "kind": kind,
                "payload": event_payload,
            }
            line = (_json(event) + "\n").encode("utf-8")
            current = path.stat().st_size if path.is_file() else 0
            if current + len(line) <= MAX_EVENT_LOG_BYTES:
                self._append_private(path, line)
            elif job_id not in self.event_truncated:
                self.event_truncated.add(job_id)
                summary["journal_truncated"] = 1
                warning = {
                    "v": 1,
                    "seq": self._next_event_seq(job_id, path),
                    "job_id": job_id,
                    "ts": now,
                    "provider": job["provider"],
                    "kind": "warning",
                    "payload": {"message": "Normalized event journal reached its byte budget"},
                }
                warning_line = (_json(warning) + "\n").encode("utf-8")
                if current + len(warning_line) <= MAX_EVENT_LOG_BYTES:
                    self._append_private(path, warning_line)
            summary["last_event_at"] = now
            summary["last_event_kind"] = kind
            if kind in SEMANTIC_PROGRESS_KINDS:
                summary["last_progress_at"] = now
            if kind == "warning" and event_payload.get("subtype"):
                summary["provider_result_error"] = 1
            if kind == "tool_started":
                tool_id = str(event_payload.get("id") or event_payload.get("name") or "tool")[:200]
                tools = self.open_tools.setdefault(job_id, {})
                if tool_id in tools or len(tools) < 256:
                    tools[tool_id] = (str(event_payload.get("name") or "tool")[:200], now)
                oldest_name = next(iter(tools.values()))[0]
                summary["open_tool"] = oldest_name
                summary["open_tool_count"] = len(tools)
                summary["open_tool_since"] = min(item[1] for item in tools.values())
            elif kind == "tool_finished":
                tools = self.open_tools.get(job_id, {})
                tool_id = str(
                    event_payload.get("id") or event_payload.get("name") or "tool"
                )[:200]
                if tools and tool_id in tools:
                    tools.pop(tool_id)
                elif tools:
                    tools.clear()
                if tools:
                    oldest_name = next(iter(tools.values()))[0]
                    summary["open_tool"] = oldest_name
                    summary["open_tool_count"] = len(tools)
                    summary["open_tool_since"] = min(item[1] for item in tools.values())
                else:
                    summary["open_tool"] = ""
                    summary["open_tool_count"] = 0
                    summary["open_tool_since"] = None
        self._persist_event_summary(job_id, force=force or kind in {
            "job_started", "tool_started", "tool_finished", "job_terminal",
        })
        self._signal_change(job_id)

    def _finish_job(
        self, job_id: str, status: str, failure_kind: str, message: str, **values: Any
    ) -> dict[str, Any]:
        self.store.update(
            job_id, status=status, failure_kind=failure_kind, message=message,
            prompt="", finished_at=_now(), notify=False, **values,
        )
        try:
            self._record_event(job_id, "job_terminal", {
                "status": status,
                "failure_kind": failure_kind,
                "message": message[:2_000],
                "exit_code": values.get("exit_code"),
            }, force=True)
        except Exception:
            self._signal_change(job_id)
        result = self.store.get(job_id)
        if job_id not in self.tasks:
            self._forget_job_state(job_id)
        return result

    def _forget_job_state(self, job_id: str) -> None:
        self.last_event_writes.pop(job_id, None)
        self.event_decoders.pop(job_id, None)
        self.event_sequences.pop(job_id, None)
        self.event_summaries.pop(job_id, None)
        self.event_truncated.discard(job_id)
        self.normalization_failed.discard(job_id)
        self.open_tools.pop(job_id, None)

    async def _append_log(self, job_id: str, stream: str, data: bytes) -> None:
        if not data:
            return
        lock = self.log_locks.setdefault(job_id, asyncio.Lock())
        prefix = f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {stream}] ".encode()
        async with lock:
            log_path = self.job_log_paths[job_id]
            semantic_stdout = (
                stream == "stdout" and self._private_semantic_stdout(self.store.get(job_id))
            )
            combined_budget = MAX_JOB_LOG_BYTES // 2
            raw_budget = MAX_JOB_LOG_BYTES - combined_budget
            combined_size = log_path.stat().st_size if log_path.exists() else 0
            combined_remaining = max(0, combined_budget - combined_size)
            truncation_marker = b"\n[output truncated at configured log budget]\n"
            if combined_remaining and not semantic_stdout:
                combined = prefix + data + (b"" if data.endswith(b"\n") else b"\n")
                with log_path.open("ab") as handle:
                    if len(combined) > combined_remaining and combined_remaining > len(truncation_marker):
                        handle.write(combined[:combined_remaining - len(truncation_marker)] + truncation_marker)
                    else:
                        handle.write(combined[:combined_remaining])
            elif not semantic_stdout and combined_size >= combined_budget and log_path.is_file():
                with log_path.open("r+b") as handle:
                    handle.seek(max(0, combined_budget - len(truncation_marker)))
                    handle.write(truncation_marker)
            stream_path = Path(f"{log_path}.{stream}")
            stdout_path = Path(f"{log_path}.stdout")
            stderr_path = Path(f"{log_path}.stderr")
            raw_size = sum(path.stat().st_size for path in (stdout_path, stderr_path) if path.exists())
            raw_remaining = max(0, raw_budget - raw_size)
            if raw_remaining:
                self._append_private(stream_path, data[:raw_remaining])
        # Wake readers for every written chunk. The database timestamp remains
        # throttled, but transport liveness must not add up to 60 seconds latency.
        self._signal_change(job_id)
        now = _now()
        if now - self.last_output_writes.get(job_id, 0) >= 1:
            self.store.update(job_id, last_output_at=now)
            self.last_output_writes[job_id] = now

    async def _normalize_provider_bytes(
        self, job_id: str, decoder: ProviderEventDecoder, data: bytes, *, final: bool = False
    ) -> None:
        try:
            for event in decoder.feed(data, final=final):
                self._record_event(job_id, event["kind"], event["payload"])
        except Exception as exc:
            self.event_decoders.pop(job_id, None)
            if job_id in self.normalization_failed:
                return
            self.normalization_failed.add(job_id)
            self.store.update(job_id, semantic_normalization_failed=1)
            message = f"Semantic event normalization disabled: {type(exc).__name__}: {exc}"
            try:
                self._record_event(job_id, "warning", {"message": message[:2_000]}, force=True)
            except Exception:
                pass
            try:
                await self._append_log(job_id, "stderr", message.encode("utf-8"))
            except Exception:
                pass

    async def _stream(self, job_id: str, stream: str, reader: asyncio.StreamReader | None) -> None:
        if reader is None:
            return
        while True:
            chunk = await reader.read(16 * 1024)
            if not chunk:
                decoder = self.event_decoders.get(job_id) if stream == "stdout" else None
                if decoder is not None:
                    await self._normalize_provider_bytes(job_id, decoder, b"", final=True)
                return
            await self._append_log(job_id, stream, chunk)
            decoder = self.event_decoders.get(job_id) if stream == "stdout" else None
            if decoder is not None:
                await self._normalize_provider_bytes(job_id, decoder, chunk)

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except Exception:
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
            return
        except asyncio.TimeoutError:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except Exception:
            proc.kill()
        await proc.wait()

    async def _ps_field(self, pid: int, field: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "ps", "-p", str(pid), "-o", f"{field}=",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
        return stdout.decode("utf-8", errors="replace").strip()

    async def _run_job(self, job_id: str) -> None:
        proc: asyncio.subprocess.Process | None = None
        streams: list[asyncio.Task[None]] = []
        try:
            job = self.store.get(job_id)
            if job["status"] != "launching":
                return
            if job["cancel_requested"]:
                self._finish_job(
                    job_id, "cancelled", "cancelled", "Cancelled before provider launch",
                )
                return
            if _now() >= float(job["created_at"]) + int(job["timeout_seconds"]):
                self._finish_job(
                    job_id, "failed", "queue_timeout",
                    "Hard deadline reached before a provider slot became available",
                )
                return
            argv, stdin_text, env = self.command_builder(job)
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=job["workdir"],
                env=env,
                stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            self.processes[job_id] = proc
            self.job_log_paths[job_id] = Path(job["log_path"])
            process_start = await self._ps_field(proc.pid, "lstart")
            live_executable = await self._ps_field(proc.pid, "comm")
            current = self.store.get(job_id)
            if current["cancel_requested"] or current["status"] != "launching":
                await self._terminate(proc)
                self._finish_job(
                    job_id, "cancelled", "cancelled", "Cancelled during provider launch",
                    exit_code=proc.returncode,
                )
                return
            started = _now()
            self.store.update(
                job_id, status="running", started_at=started, last_output_at=started,
                pid=proc.pid, pgid=proc.pid,
                binary_path=str(Path(live_executable or argv[0]).resolve()),
                process_start=process_start, prompt="", message="",
            )
            if self._has_semantic_adapter(job):
                self.event_decoders[job_id] = ProviderEventDecoder(str(job["provider"]))
            try:
                self._record_event(job_id, "job_started", {"pid": proc.pid}, force=True)
            except Exception:
                self.normalization_failed.add(job_id)
                self.event_decoders.pop(job_id, None)
                self._signal_change(job_id)
            if stdin_text is not None and proc.stdin is not None:
                proc.stdin.write(stdin_text.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
            streams = [
                asyncio.create_task(self._stream(job_id, "stdout", proc.stdout)),
                asyncio.create_task(self._stream(job_id, "stderr", proc.stderr)),
            ]
            deadline = float(job["created_at"]) + int(job["timeout_seconds"])
            outcome = "completed"
            failure_kind = ""
            message = ""
            next_heartbeat = 0.0
            while proc.returncode is None:
                current = self.store.get(job_id)
                if current["cancel_requested"]:
                    outcome, failure_kind, message = "cancelled", "cancelled", "Cancelled by caller"
                    await self._terminate(proc)
                    break
                if _now() >= deadline:
                    outcome, failure_kind = "failed", "timeout"
                    message = f"Hard deadline reached after {job['timeout_seconds']} seconds"
                    await self._terminate(proc)
                    break
                if _now() >= next_heartbeat:
                    self.store.touch(job_id)
                    next_heartbeat = _now() + 5
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass
            if proc.returncode is None:
                await proc.wait()
            await asyncio.gather(*streams, return_exceptions=True)
            if outcome == "completed" and proc.returncode != 0:
                outcome, failure_kind, message = "failed", "provider_exit", f"Provider exited with code {proc.returncode}"
            self._finish_job(
                job_id, outcome, failure_kind, message, exit_code=proc.returncode,
            )
        except asyncio.CancelledError:
            if proc is not None:
                await self._terminate(proc)
            self._finish_job(
                job_id, "interrupted", "supervisor_shutdown",
                "Supervisor stopped while the job was running",
            )
            raise
        except Exception as exc:
            if proc is not None:
                await self._terminate(proc)
            self._finish_job(job_id, "failed", "launch_error", str(exc))
        finally:
            self.change_events.pop(job_id, None)
            self.processes.pop(job_id, None)
            self.tasks.pop(job_id, None)
            self.log_locks.pop(job_id, None)
            self.last_output_writes.pop(job_id, None)
            self.job_log_paths.pop(job_id, None)
            self._forget_job_state(job_id)

    async def _scheduler(self) -> None:
        next_prune = 0.0
        while not self._stopping:
            try:
                if _now() >= next_prune:
                    for base in self.store.prune(_now() - JOB_RETENTION_SECONDS):
                        for candidate in (
                            Path(base), Path(f"{base}.stdout"), Path(f"{base}.stderr"),
                            Path(f"{base}.events.jsonl"), Path(f"{base}.partial.txt"),
                        ):
                            try:
                                candidate.unlink()
                            except FileNotFoundError:
                                pass
                    next_prune = _now() + 3600
                active = Counter(self.store.get(job_id)["provider"] for job_id in self.tasks)
                for job in self.store.queued():
                    job_id = job["job_id"]
                    provider = job["provider"]
                    if job_id in self.tasks or active[provider] >= max(1, self.provider_limits[provider]):
                        continue
                    if not self.store.claim(job_id):
                        continue
                    task = asyncio.create_task(self._run_job(job_id))
                    self.tasks[job_id] = task
                    active[provider] += 1
            except Exception as exc:
                print(f"agent-job scheduler error: {exc}", file=sys.stderr, flush=True)
                await asyncio.sleep(1)
                continue
            await asyncio.sleep(0.25)

    async def _cleanup_interrupted(self) -> None:
        for job in self.store.reconcile():
            try:
                self._record_event(job["job_id"], "job_terminal", {
                    "status": "interrupted",
                    "failure_kind": "supervisor_restart",
                    "message": "Supervisor restarted while the job was running",
                }, force=True)
            except Exception:
                self._signal_change(job["job_id"])
            finally:
                self._forget_job_state(job["job_id"])
            pgid = job.get("pgid")
            if not pgid:
                continue
            pid = int(job.get("pid") or 0)
            try:
                process_start = await self._ps_field(pid, "lstart")
                live_pgid = await self._ps_field(pid, "pgid")
                executable = await self._ps_field(pid, "comm")
            except (OSError, ValueError):
                continue
            if (
                not process_start
                or process_start != str(job.get("process_start") or "")
                or int(live_pgid or 0) != int(pgid)
                or not executable
                or Path(executable).expanduser().resolve() != Path(str(job.get("binary_path") or "")).expanduser().resolve()
            ):
                self.store.update(job["job_id"], message="Restart cleanup skipped: process identity did not match")
                continue
            try:
                os.killpg(int(pgid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                continue
            await asyncio.sleep(1)
            try:
                os.killpg(int(pgid), 0)
                os.killpg(int(pgid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        if int(payload.get("caller_depth") or 0) > 0:
            raise RuntimeError("Recursive cross-agent delegation is not allowed")
        provider = str(payload.get("provider") or "")
        mode = str(payload.get("mode") or "")
        requested_model = str(payload.get("model") or "").strip()
        prompt = str(payload.get("prompt") or "")
        owner = str(payload.get("owner") or "")[:200]
        if provider not in self.provider_limits:
            raise ValueError(f"Unsupported provider: {provider}")
        execution_backend = _execution_backend(provider, owner)
        if execution_backend != "cao":
            self.binary_finder(provider)
        if mode not in {"readonly", "implement"}:
            raise ValueError(f"Unsupported mode: {mode}")
        if mode == "implement":
            if os.environ.get("AGENT_JOB_ALLOW_IMPLEMENT") != "1":
                raise PermissionError("Durable implement mode is disabled by supervisor policy")
            try:
                expected_capability = IMPLEMENT_TOKEN_PATH.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise PermissionError("Implementation capability is unavailable") from exc
            supplied_capability = str(payload.get("implement_capability") or "")
            if not expected_capability or not hmac.compare_digest(supplied_capability, expected_capability):
                raise PermissionError("Invalid implementation capability")
        if requested_model and not MODEL_PATTERN.fullmatch(requested_model):
            raise ValueError("Model contains unsupported characters")
        model, model_message = _normalize_model(provider, requested_model)
        if not model:
            raise ValueError("Model is required for this provider")
        if not MODEL_PATTERN.fullmatch(model):
            raise ValueError("Effective model contains unsupported characters")
        if not prompt.strip() or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise ValueError(f"Prompt must contain 1 to {MAX_PROMPT_BYTES} UTF-8 bytes")
        workdir = _safe_workdir(str(payload.get("workdir") or ""))
        timeout = max(MIN_TIMEOUT_SECONDS, min(int(payload.get("timeout_seconds") or 2700), MAX_TIMEOUT_SECONDS))
        requested_max_turns = int(payload.get("max_turns") or 0)
        max_turns = 0 if requested_max_turns <= 0 else min(requested_max_turns, 10_000)
        if execution_backend == "cao" and mode == "readonly" and provider == "codex":
            raise ValueError("CAO cannot enforce read-only Codex execution; use the native backend")
        if execution_backend == "cao" and max_turns > 0:
            raise ValueError("CAO does not support an explicit provider turn ceiling")
        soft_stall = max(30, min(int(payload.get("soft_stall_seconds") or DEFAULT_SOFT_STALL_SECONDS), timeout))
        spec = {
            "provider": provider, "model": model, "requested_model": requested_model,
            "mode": mode, "workdir": str(workdir),
            "prompt": prompt, "owner": owner, "message": model_message,
            "timeout_seconds": timeout, "soft_stall_seconds": soft_stall, "max_turns": max_turns,
            "execution_backend": execution_backend,
            "semantic_stream": int(
                execution_backend == "native"
                and provider in SEMANTIC_PROVIDERS
                and (provider != "kimi" or _kimi_semantic_enabled())
            ),
            "idempotency_key": str(payload.get("idempotency_key") or "")[:200],
        }
        hash_fields = {key: spec[key] for key in (
            "provider", "model", "requested_model", "mode", "workdir", "prompt",
            "timeout_seconds", "max_turns",
            "execution_backend",
            "semantic_stream",
        )}
        spec["request_hash"] = hashlib.sha256(_json(hash_fields).encode("utf-8")).hexdigest()
        job_id = str(uuid.uuid4())
        log_path = self.log_dir / f"{job_id}.log"
        job = self.store.create(spec, job_id, log_path)
        return self._public(self.store.get(job["job_id"]))

    def route_decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        from agent_routing_policy import MAX_INTENT_BYTES, decide, normalize_intent

        intent = {key: value for key, value in payload.items() if key != "action"}
        owner = str(intent.pop("owner", "") or "")[:200]
        if len(_json(intent).encode("utf-8")) > MAX_INTENT_BYTES:
            raise ValueError(f"Routing intent exceeds {MAX_INTENT_BYTES} UTF-8 bytes")
        canonical_intent = normalize_intent(intent)
        decision = decide(canonical_intent)
        return self.store.create_route_decision(canonical_intent, decision, owner)

    def _read_events(
        self, job: dict[str, Any], cursor: int, max_bytes: int
    ) -> tuple[int, int, list[dict[str, Any]]]:
        path = self._event_path(job)
        size = path.stat().st_size if path.is_file() else 0
        cursor = min(max(0, cursor), size)
        if not path.is_file() or cursor >= size:
            return cursor, size, []
        events: list[dict[str, Any]] = []
        consumed = 0
        next_cursor = cursor
        with path.open("rb") as handle:
            handle.seek(cursor)
            while consumed < max_bytes or not events:
                line = handle.readline(MAX_EVENT_RECORD_BYTES + 1)
                if not line:
                    break
                if not line.endswith(b"\n"):
                    skipped = len(line)
                    while line and not line.endswith(b"\n"):
                        line = handle.readline(MAX_EVENT_RECORD_BYTES + 1)
                        skipped += len(line)
                    consumed += skipped
                    next_cursor += skipped
                    events.append({
                        "v": 1,
                        "seq": None,
                        "job_id": job["job_id"],
                        "ts": _now(),
                        "provider": job["provider"],
                        "kind": "truncated_event",
                        "source": "reader",
                        "payload": {
                            "message": "Oversized normalized event was skipped",
                            "bytes_skipped": skipped,
                        },
                    })
                    continue
                consumed += len(line)
                next_cursor += len(line)
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    value = {
                        "v": 1,
                        "seq": None,
                        "job_id": job["job_id"],
                        "ts": _now(),
                        "provider": job["provider"],
                        "kind": "parse_error",
                        "source": "reader",
                        "payload": {"message": "Corrupt normalized event journal entry"},
                    }
                events.append(value)
        return next_cursor, size, events

    def read(self, payload: dict[str, Any]) -> dict[str, Any]:
        job = self.store.get(str(payload.get("job_id") or ""))
        cursor = max(0, int(payload.get("cursor") or 0))
        include_events = "event_cursor" in payload
        event_cursor = max(0, int(payload.get("event_cursor") or 0))
        max_bytes = max(1, min(int(payload.get("max_bytes") or 64_000), MAX_READ_BYTES))
        terminal = job["status"] in TERMINAL_STATUSES
        output_budget = max_bytes if not terminal else max(1, max_bytes // 2)
        path = Path(job["log_path"])
        output = ""
        next_cursor = cursor
        if path.is_file():
            size = path.stat().st_size
            cursor = min(cursor, size)
            with path.open("rb") as handle:
                handle.seek(cursor)
                data = handle.read(output_budget)
            output = data.decode("utf-8", errors="replace")
            next_cursor = cursor + len(data)
        if include_events:
            next_event_cursor, event_size, events = self._read_events(job, event_cursor, max_bytes)
        else:
            event_path = self._event_path(job)
            event_size = event_path.stat().st_size if event_path.is_file() else 0
            next_event_cursor, events = event_size, []
        result = {
            "job": self._public(job),
            "cursor": next_cursor,
            "output": output,
            "event_cursor": next_event_cursor,
            "event_size": event_size,
            "events": events,
        }
        if payload.get("stream_cursors"):
            for stream in ("stdout", "stderr"):
                stream_cursor = max(0, int(payload.get(f"{stream}_cursor") or 0))
                stream_path = Path(f"{job['log_path']}.{stream}")
                size = stream_path.stat().st_size if stream_path.is_file() else 0
                stream_cursor = min(stream_cursor, size)
                data = b""
                expose_stream = not (
                    stream == "stdout" and self._private_semantic_stdout(job)
                )
                if stream_path.is_file() and expose_stream:
                    with stream_path.open("rb") as handle:
                        handle.seek(stream_cursor)
                        data = handle.read(max_bytes)
                result[f"{stream}_output"] = data.decode("utf-8", errors="replace")
                result[f"{stream}_cursor"] = (
                    stream_cursor + len(data) if expose_stream else size
                )
                result[f"{stream}_size"] = size
        if terminal:
            stream_budget = max(1, max_bytes // 4)
            for stream in ("stdout", "stderr"):
                stream_path = Path(f"{job['log_path']}.{stream}")
                expose_stream = not (
                    stream == "stdout" and self._private_semantic_stdout(job)
                )
                if stream_path.is_file() and expose_stream:
                    size = stream_path.stat().st_size
                    with stream_path.open("rb") as handle:
                        handle.seek(max(0, size - stream_budget))
                        data = handle.read(stream_budget)
                    result[stream] = data.decode("utf-8", errors="replace")
                else:
                    result[stream] = ""
            partial_path = self._partial_path(job)
            if partial_path.is_file():
                result["partial_response"] = partial_path.read_text(
                    encoding="utf-8", errors="replace"
                )
            else:
                result["partial_response"] = ""
            if not self._semantic_adapter_active(job):
                result["partial_result_state"] = "unavailable"
            elif result["partial_response"] and job.get("partial_response_truncated"):
                result["partial_result_state"] = "truncated"
            elif (
                job["status"] == "completed"
                and result["partial_response"]
                and not job.get("provider_result_error")
            ):
                result["partial_result_state"] = "complete"
            elif result["partial_response"]:
                result["partial_result_state"] = "partial"
            else:
                result["partial_result_state"] = "none"
        return result

    async def read_wait(self, payload: dict[str, Any]) -> dict[str, Any]:
        wait_seconds = max(0, min(int(payload.get("wait_seconds") or 0), 60))
        result = self.read(payload)
        if (
            wait_seconds == 0
            or result["output"]
            or result["events"]
            or result["job"]["lifecycle_status"] in TERMINAL_STATUSES
        ):
            return result
        initial = result["job"]
        fingerprint = (
            initial["status"], initial.get("last_output_at"), initial.get("last_event_at"),
            initial.get("last_event_kind"), initial.get("open_tool"),
        )
        deadline = time.monotonic() + wait_seconds
        job_id = str(payload.get("job_id") or "")
        event = self.change_events.setdefault(job_id, asyncio.Event())
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return result
                event.clear()
                result = self.read(payload)
                current = result["job"]
                current_fingerprint = (
                    current["status"], current.get("last_output_at"), current.get("last_event_at"),
                    current.get("last_event_kind"), current.get("open_tool"),
                )
                if (
                    result["output"]
                    or result["events"]
                    or current["lifecycle_status"] in TERMINAL_STATUSES
                    or current_fingerprint != fingerprint
                ):
                    return result
                try:
                    await asyncio.wait_for(event.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    return self.read(payload)
        finally:
            if (
                self.store.get(job_id)["status"] in TERMINAL_STATUSES
                and self.change_events.get(job_id) is event
            ):
                self.change_events.pop(job_id, None)

    def inbox(self, payload: dict[str, Any]) -> dict[str, Any]:
        owner = str(payload.get("owner") or "")[:200]
        if not owner:
            raise ValueError("Inbox owner is required")
        limit = max(1, min(int(payload.get("limit") or 20), 100))
        raw_ack = payload.get("ack_delivery_ids") or []
        if not isinstance(raw_ack, list) or any(not isinstance(item, str) for item in raw_ack):
            raise ValueError("ack_delivery_ids must be a list of strings")
        deliveries = []
        for row in self.store.inbox(owner, limit, raw_ack):
            delivery_id = row.pop("delivery_id")
            created_at = row.pop("delivery_created_at")
            deliveries.append({
                "delivery_id": delivery_id,
                "created_at": created_at,
                "job": self._public(row),
            })
        return {"deliveries": deliveries}

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=10)
            if len(raw) > MAX_PROMPT_BYTES + 64_000:
                raise ValueError("Request is too large")
            payload = json.loads(raw.decode("utf-8"))
            action = payload.get("action")
            if action == "ping":
                result = {"status": "ok", "pid": os.getpid(), "socket": str(self.socket_path)}
            elif action == "submit":
                result = self.submit(payload)
            elif action == "route_decide":
                result = self.route_decide(payload)
            elif action == "read":
                result = await self.read_wait(payload)
            elif action == "list":
                limit = max(1, min(int(payload.get("limit") or 50), 200))
                requested_status = str(payload.get("status") or "")
                owner = str(payload.get("owner") or "")[:200]
                jobs = [self._public(job) for job in self.store.list(requested_status, limit, owner)]
                if requested_status == "possibly_stalled":
                    jobs = [job for job in jobs if job["status"] == "possibly_stalled"][:limit]
                result = {"jobs": jobs}
            elif action == "cancel":
                job_id = str(payload.get("job_id") or "")
                job = self.store.get(job_id)
                if job["status"] not in TERMINAL_STATUSES:
                    job = self.store.update(job_id, cancel_requested=1, message="Cancellation requested")
                    if job["status"] == "queued":
                        job = self._finish_job(
                            job_id, "cancelled", "cancelled", "Cancelled before launch"
                        )
                        # The terminal update has already signalled existing waiters. Remove
                        # the registry entry because queued jobs never enter _run_job's cleanup.
                        self.change_events.pop(job_id, None)
                result = self._public(job)
            elif action == "inbox":
                result = self.inbox(payload)
            else:
                raise ValueError(f"Unsupported action: {action}")
            response = {"ok": True, "result": result}
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        writer.write((_json(response) + "\n").encode("utf-8"))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def serve(self) -> None:
        lock_path = self.state_dir / "supervisor.lock"
        self._lock_handle = lock_path.open("a+")
        try:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunning("Another agent job supervisor already owns the state directory") from exc
        await self._cleanup_interrupted()
        if self.socket_path.exists():
            self.socket_path.unlink()
        server = await asyncio.start_unix_server(
            self.handle, path=str(self.socket_path), limit=(2 * MAX_PROMPT_BYTES) + 64_000
        )
        os.chmod(self.socket_path, stat.S_IRUSR | stat.S_IWUSR)
        scheduler = asyncio.create_task(self._scheduler())
        try:
            async with server:
                await server.serve_forever()
        finally:
            self._stopping = True
            scheduler.cancel()
            await asyncio.gather(scheduler, return_exceptions=True)
            for task in list(self.tasks.values()):
                task.cancel()
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
            if self.socket_path.exists():
                self.socket_path.unlink()
            if self._lock_handle is not None:
                fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
                self._lock_handle.close()
                self._lock_handle = None


async def _run_supervisor() -> None:
    supervisor = Supervisor()
    task = asyncio.create_task(supervisor.serve())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, task.cancel)
        except NotImplementedError:
            pass
    try:
        await task
    except asyncio.CancelledError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("serve",), nargs="?", default="serve")
    args = parser.parse_args()
    del args
    try:
        asyncio.run(_run_supervisor())
    except KeyboardInterrupt:
        return 0
    except AlreadyRunning as exc:
        print(str(exc), file=sys.stderr)
        return 75
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
