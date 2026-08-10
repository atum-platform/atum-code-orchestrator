#!/usr/bin/env python3
"""Run one supervisor-owned job through CAO while preserving stdout semantics."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PROVIDERS = {"claude": "claude_code", "kimi": "kimi_cli", "codex": "codex"}
TERMINAL = {"completed", "error"}
POLL_RETRIES = 5
MAX_REQUEST_TIMEOUT = 8.0


class StopRequested(Exception):
    """Interrupt a blocking CAO request so the session cleanup path can run."""


class CaoClient:
    def __init__(self, base_url: str, token: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(
        self, method: str, path: str, *, params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None, timeout: float = 15.0,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"CAO HTTP {exc.code}: {detail}") from exc
        except (URLError, OSError) as exc:
            raise RuntimeError(f"CAO unavailable at {self.base_url}: {exc}") from exc
        return json.loads(payload) if payload else {}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=sorted(PROVIDERS), required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--mode", choices=("readonly", "implement"), required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--job-id", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    prompt = sys.stdin.read()
    if not prompt.strip():
        print("CAO bridge received an empty prompt", file=sys.stderr)
        return 2
    client = CaoClient(
        os.environ.get("AGENT_JOB_CAO_URL", "http://127.0.0.1:9889"),
        os.environ.get("AGENT_JOB_CAO_TOKEN", ""),
    )
    stop = False
    safe_job_id = "".join(char if char.isalnum() or char in "-_" else "-" for char in args.job_id)
    session_name = f"agent-job-{safe_job_id}"
    now = time.time()
    try:
        requested_deadline = float(os.environ.get("AGENT_JOB_DEADLINE_EPOCH", now + 7200.0))
    except ValueError:
        requested_deadline = now + 7200.0
    deadline = max(
        now + 30.0,
        min(requested_deadline, now + 7200.0),
    )
    actual_session = ""
    launch_attempted = False

    def request_stop(_signum, _frame) -> None:
        nonlocal stop
        stop = True
        raise StopRequested

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    params: dict[str, Any] = {
        "provider": PROVIDERS[args.provider],
        "agent_profile": "reviewer" if args.mode == "readonly" else "developer",
        "session_name": session_name,
        "working_directory": args.workdir,
    }
    if args.model:
        params["model"] = args.model
    if args.mode == "readonly":
        params["allowed_tools"] = "fs_read,fs_list"
    try:
        launch_attempted = True
        terminal = client.request(
            "POST",
            "/sessions",
            params=params,
            body={
                "initial_message": prompt,
                "env_vars": {
                    "AGENT_JOB_DEPTH": os.environ.get("AGENT_JOB_DEPTH", "1"),
                    "AGENT_JOB_PROVIDER": args.provider,
                    "AGENT_JOB_ID": args.job_id,
                    "AGENT_JOB_MODE": args.mode,
                    "AGENT_JOB_DEADLINE_EPOCH": str(deadline),
                },
                "metadata": {
                    "kind": "agent_job_compat",
                    "job_id": args.job_id,
                    "mode": args.mode,
                    "provider": args.provider,
                    "expires_at": deadline,
                },
            },
            timeout=min(
                MAX_REQUEST_TIMEOUT,
                max(0.1, float(os.environ.get("AGENT_JOB_CAO_LAUNCH_TIMEOUT", "8"))),
            ),
        )
        actual_session = str(terminal.get("session_name") or session_name)
        terminal_id = str(terminal.get("id") or "")
        if not terminal_id:
            raise RuntimeError("CAO create-session response did not include a terminal id")
        if args.mode == "readonly" and terminal.get("allowed_tools") != ["fs_read", "fs_list"]:
            raise RuntimeError(
                "CAO read-only tool policy mismatch: expected ['fs_read', 'fs_list'], "
                f"got {terminal.get('allowed_tools')!r}"
            )
        working_directory = client.request(
            "GET", f"/terminals/{terminal_id}/working-directory", timeout=MAX_REQUEST_TIMEOUT
        ).get("working_directory")
        if not working_directory or Path(working_directory).resolve() != Path(args.workdir).resolve():
            raise RuntimeError(
                f"CAO workspace mismatch: requested {args.workdir}, got {working_directory!r}"
            )
        prior_status = ""
        poll_failures = 0
        while not stop:
            try:
                current = client.request(
                    "GET", f"/terminals/{terminal_id}", timeout=MAX_REQUEST_TIMEOUT
                )
                poll_failures = 0
            except RuntimeError as exc:
                if "CAO unavailable" not in str(exc) or poll_failures >= POLL_RETRIES:
                    raise
                poll_failures += 1
                delay = min(8.0, float(2 ** (poll_failures - 1)))
                print(f"[CAO] poll unavailable; retry {poll_failures}/{POLL_RETRIES}", flush=True)
                time.sleep(delay)
                continue
            status = current.get("status", "unknown")
            if status != prior_status:
                print(f"[CAO] {actual_session}: {status}", flush=True)
                prior_status = status
            if status in TERMINAL:
                final = client.request(
                    "GET", f"/terminals/{terminal_id}/output",
                    params={"mode": "last"}, timeout=MAX_REQUEST_TIMEOUT,
                ).get("output", "")
                if not str(final).strip():
                    raise RuntimeError(f"CAO terminal reached {status} without a retained result")
                print(final, flush=True)
                return 0 if status == "completed" else 1
            time.sleep(0.5)
    except StopRequested:
        return 130
    except Exception as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 1
    finally:
        if launch_attempted:
            try:
                client.request(
                    "DELETE", f"/sessions/{actual_session or session_name}", timeout=3.0
                )
            except Exception as exc:
                print(f"CAO session cleanup failed: {exc}", file=sys.stderr, flush=True)
    return 130


if __name__ == "__main__":
    raise SystemExit(main())
