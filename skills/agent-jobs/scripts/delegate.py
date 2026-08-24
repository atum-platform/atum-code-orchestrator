#!/usr/bin/env python3
"""Run one fresh scoped implementation job through the durable supervisor."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import time


_override = os.environ.get("AGENT_JOB_TOOLS_DIR", "").strip()
_candidates = [
    Path(_override).expanduser() if _override else None,
    Path(__file__).resolve().parents[3] / "tools",
    Path.home() / ".local/share/atum-agent-jobs/tools",
]
TOOLS = next((path for path in _candidates if path and (path / "agent_job_client.py").is_file()), None)
if TOOLS is None:
    raise SystemExit("agent-jobs tools not found; set AGENT_JOB_TOOLS_DIR to the repository tools directory")
sys.path.insert(0, str(TOOLS))

from agent_job_client import cancel, read, submit  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("claude", "kimi", "codex"), required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--mode", choices=("implement", "readonly"), required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--queue-timeout-seconds", type=int, default=900)
    parser.add_argument("--run-timeout-seconds", type=int, default=2700)
    parser.add_argument(
        "--timeout-seconds", type=int,
        help="deprecated alias for --run-timeout-seconds",
    )
    args = parser.parse_args()
    if args.provider != "kimi" and not args.model:
        parser.error("--model is required unless --provider=kimi")
    workdir = Path(args.workdir).expanduser().resolve()
    if not workdir.is_dir():
        parser.error(f"workdir is not an existing directory: {workdir}")
    if int(os.environ.get("AGENT_JOB_DEPTH", "0") or 0) > 0:
        print("Recursive cross-agent delegation is not allowed", file=sys.stderr)
        return 2
    job_id = ""
    try:
        job = submit(
            provider=args.provider, model=args.model, mode=args.mode,
            workdir=str(workdir), prompt=args.prompt,
            queue_timeout_seconds=args.queue_timeout_seconds,
            run_timeout_seconds=(
                args.timeout_seconds if args.timeout_seconds is not None
                else args.run_timeout_seconds
            ),
            owner="agent-jobs:delegate",
        )
        job_id = str(job["job_id"])
        print(f"AGENT_JOB_ID={job_id}", file=sys.stderr, flush=True)
        cursor = 0
        event_cursor = 0
        emitted = ""
        while True:
            result = read(
                job_id, cursor=cursor, event_cursor=event_cursor, wait_seconds=30
            )
            cursor = int(result["cursor"])
            event_cursor = int(result["event_cursor"])
            semantic = bool(result["job"].get("semantic_stream")) and not result["job"].get(
                "semantic_normalization_failed"
            )
            if semantic:
                for event in result.get("events") or []:
                    payload = event.get("payload") or {}
                    if (
                        event.get("kind") == "warning"
                        and payload.get("subtype") == "suspected_response_loss"
                    ):
                        print(
                            "Warning: Claude may have returned more answer text than was streamed; "
                            "the retained response is marked partial.",
                            file=sys.stderr,
                            flush=True,
                        )
                        continue
                    if event.get("kind") != "message_delta":
                        continue
                    text = str(payload.get("text") or "")
                    if text:
                        print(text, end="", flush=True)
                        emitted += text
            elif result.get("output"):
                print(str(result["output"]), end="", flush=True)
            current = result["job"]
            if current["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                partial = str(result.get("partial_response") or "")
                if semantic and partial:
                    remainder = partial[len(emitted):] if partial.startswith(emitted) else partial
                    if remainder:
                        print(remainder, end="", flush=True)
                if current["status"] == "completed":
                    return 0
                print(
                    f"Delegation {current['status']}: "
                    f"{current.get('message') or current.get('failure_kind')}",
                    file=sys.stderr,
                )
                return 124 if current.get("failure_kind") == "timeout" else 1
            time.sleep(1)
    except KeyboardInterrupt:
        if job_id:
            cancel(job_id)
        return 130
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
