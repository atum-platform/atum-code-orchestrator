#!/usr/bin/env python3
"""Client and CLI for the local cross-agent job supervisor."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sys
import time
from typing import Any
import uuid


DEFAULT_STATE_DIR = Path(
    os.environ.get("AGENT_JOB_STATE_DIR", "~/.local/state/agent-job-supervisor")
).expanduser()
DEFAULT_SOCKET_PATH = Path(
    os.environ.get("AGENT_JOB_SOCKET", str(DEFAULT_STATE_DIR / "supervisor.sock"))
).expanduser()
DEFAULT_IMPLEMENT_TOKEN_PATH = Path(
    os.environ.get("AGENT_JOB_IMPLEMENT_TOKEN_FILE", str(DEFAULT_STATE_DIR / "implement.token"))
).expanduser()


class SupervisorUnavailable(RuntimeError):
    pass


def request(payload: dict[str, Any], socket_path: Path | str = DEFAULT_SOCKET_PATH) -> dict[str, Any]:
    path = Path(socket_path).expanduser()
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    chunks: list[bytes] = []
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(15 + max(0, min(int(payload.get("wait_seconds") or 0), 60)))
            client.connect(str(path))
            client.sendall(data)
            client.shutdown(socket.SHUT_WR)
            while True:
                chunk = client.recv(64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as exc:
        raise SupervisorUnavailable(f"Agent job supervisor is unavailable at {path}: {exc}") from exc
    try:
        response = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Agent job supervisor returned an invalid response") from exc
    if not response.get("ok"):
        raise RuntimeError(str(response.get("error") or "Agent job supervisor request failed"))
    return dict(response.get("result") or {})


def submit(socket_path: Path | str = DEFAULT_SOCKET_PATH, **kwargs: Any) -> dict[str, Any]:
    if kwargs.get("workdir"):
        kwargs["workdir"] = str(Path(str(kwargs["workdir"])).expanduser().resolve())
    mode = str(kwargs.get("mode") or "")
    capability = ""
    if mode == "implement":
        try:
            capability = DEFAULT_IMPLEMENT_TOKEN_PATH.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise PermissionError(
                f"Implementation capability is unavailable at {DEFAULT_IMPLEMENT_TOKEN_PATH}"
            ) from exc
        if not capability:
            raise PermissionError("Implementation capability is empty")
    payload = {
        "action": "submit",
        "caller_depth": int(kwargs.pop("caller_depth", os.environ.get("AGENT_JOB_DEPTH", "0")) or 0),
        "idempotency_key": str(kwargs.pop("idempotency_key", "") or uuid.uuid4()),
        "implement_capability": capability,
        **kwargs,
    }
    for attempt in range(2):
        try:
            return request(payload, socket_path)
        except SupervisorUnavailable:
            if attempt:
                raise
            time.sleep(.2)
    raise AssertionError("unreachable")


def route_decide(socket_path: Path | str = DEFAULT_SOCKET_PATH, **intent: Any) -> dict[str, Any]:
    payload = {"action": "route_decide", **intent}
    try:
        return request(payload, socket_path)
    except RuntimeError as exc:
        if payload.get("protocol_version") != 2 or "protocol version" not in str(exc).lower():
            raise
        return request({**payload, "protocol_version": 1}, socket_path)


def route_feedback(
    decision_id: str, session_id: str, outcome: str,
    socket_path: Path | str = DEFAULT_SOCKET_PATH,
) -> dict[str, Any]:
    return request({
        "action": "route_feedback", "decision_id": decision_id,
        "session_id": session_id, "outcome": outcome,
    }, socket_path)


def route_reconcile(
    session_id: str, active_decision_ids: list[str],
    socket_path: Path | str = DEFAULT_SOCKET_PATH,
) -> dict[str, Any]:
    return request({
        "action": "route_reconcile", "session_id": session_id,
        "active_decision_ids": active_decision_ids,
    }, socket_path)


def route_status(socket_path: Path | str = DEFAULT_SOCKET_PATH) -> dict[str, Any]:
    return request({"action": "route_status"}, socket_path)


def read(
    job_id: str,
    cursor: int = 0,
    max_bytes: int = 64_000,
    *,
    event_cursor: int | None = None,
    stream_cursors: bool = False,
    stdout_cursor: int = 0,
    stderr_cursor: int = 0,
    wait_seconds: int = 0,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": "read", "job_id": job_id, "cursor": cursor,
        "max_bytes": max_bytes,
        "stream_cursors": stream_cursors, "stdout_cursor": stdout_cursor,
        "stderr_cursor": stderr_cursor, "wait_seconds": wait_seconds,
    }
    if event_cursor is not None:
        payload["event_cursor"] = event_cursor
    return request(payload)


def list_jobs(status: str = "", limit: int = 50, owner: str = "") -> dict[str, Any]:
    return request({"action": "list", "status": status, "limit": limit, "owner": owner})


def cancel(job_id: str) -> dict[str, Any]:
    return request({"action": "cancel", "job_id": job_id})


def inbox(owner: str, limit: int = 20, ack_delivery_ids: list[str] | None = None) -> dict[str, Any]:
    return request({
        "action": "inbox", "owner": owner, "limit": limit,
        "ack_delivery_ids": ack_delivery_ids or [],
    })


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default=str(DEFAULT_SOCKET_PATH))
    sub = parser.add_subparsers(dest="action", required=True)
    submit_parser = sub.add_parser("submit")
    submit_parser.add_argument("--provider", choices=("claude", "kimi", "codex"), required=True)
    submit_parser.add_argument("--model", default="")
    submit_parser.add_argument("--mode", choices=("readonly", "implement"), required=True)
    submit_parser.add_argument("--workdir", required=True)
    submit_parser.add_argument("--prompt", required=True)
    submit_parser.add_argument("--queue-timeout-seconds", type=int, default=900)
    submit_parser.add_argument("--run-timeout-seconds", type=int, default=2700)
    submit_parser.add_argument(
        "--timeout-seconds", type=int,
        help="deprecated alias for --run-timeout-seconds",
    )
    submit_parser.add_argument("--owner", default="")
    submit_parser.add_argument("--idempotency-key", default="")
    read_parser = sub.add_parser("read")
    read_parser.add_argument("job_id")
    read_parser.add_argument("--cursor", type=int, default=0)
    read_parser.add_argument("--event-cursor", type=int, default=argparse.SUPPRESS)
    read_parser.add_argument("--max-bytes", type=int, default=64_000)
    read_parser.add_argument("--wait-seconds", type=int, default=0)
    list_parser = sub.add_parser("list")
    list_parser.add_argument("--status", default="")
    list_parser.add_argument("--limit", type=int, default=50)
    list_parser.add_argument("--owner", default="")
    cancel_parser = sub.add_parser("cancel")
    cancel_parser.add_argument("job_id")
    inbox_parser = sub.add_parser("inbox")
    inbox_parser.add_argument("--owner", required=True)
    inbox_parser.add_argument("--limit", type=int, default=20)
    inbox_parser.add_argument("--ack-delivery-id", action="append", dest="ack_delivery_ids")
    route = sub.add_parser("route-decide")
    route.add_argument("--protocol-version", type=int, default=2)
    route.add_argument("--caller-provider", required=True)
    route.add_argument("--surface", required=True)
    route.add_argument("--capability", required=True)
    route.add_argument("--complexity", default="standard")
    route.add_argument("--risk", default="medium")
    route.add_argument("--scope", default="single_module")
    route.add_argument("--duration", default="medium")
    route.add_argument("--durability", default="session")
    route.add_argument("--parallelizable", action="store_true")
    route.add_argument("--surface-capabilities", type=json.loads, default={})
    route.add_argument("--explicit-provider", default="")
    route.add_argument("--explicit-model", default="")
    route.add_argument("--session-id", default="")
    route.add_argument("--previous-decision-id", default="")
    route.add_argument(
        "--escalation-reason",
        choices=("provider_failure", "rate_limit", "unusable_output", "scope_growth", "capability_mismatch"),
        default="",
    )
    route.add_argument("--escalation-evidence", default="")
    route.add_argument("--owner", default="")
    feedback = sub.add_parser("route-feedback")
    feedback.add_argument("decision_id")
    feedback.add_argument("--session-id", required=True)
    feedback.add_argument(
        "--outcome",
        choices=("completed", "failed", "abandoned", "escalated", "not_started"),
        required=True,
    )
    reconcile = sub.add_parser("route-reconcile")
    reconcile.add_argument("--session-id", required=True)
    reconcile.add_argument("--active-decision-id", action="append", default=[])
    sub.add_parser("route-status")
    sub.add_parser("ping")
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if args.action == "submit" and args.provider != "kimi" and not args.model:
        parser.error("--model is required unless --provider=kimi")
    payload = vars(args).copy()
    payload.pop("socket")
    action = payload.pop("action")
    try:
        if action == "submit":
            result = submit(socket_path=args.socket, **payload)
        elif action == "route-decide":
            result = route_decide(socket_path=args.socket, **payload)
        elif action == "route-feedback":
            result = route_feedback(socket_path=args.socket, **payload)
        elif action == "route-reconcile":
            payload["active_decision_ids"] = payload.pop("active_decision_id")
            result = route_reconcile(socket_path=args.socket, **payload)
        elif action == "route-status":
            result = route_status(socket_path=args.socket)
        else:
            result = request({"action": action, **payload}, args.socket)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
