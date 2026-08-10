#!/usr/bin/env python3
"""Run the guarded review CLI from an installed or copied agent-jobs skill."""

from pathlib import Path
import os
import sys


_override = os.environ.get("AGENT_JOB_TOOLS_DIR", "").strip()
_candidates = [
    Path(_override).expanduser() if _override else None,
    Path(__file__).resolve().parents[3] / "tools",
    Path.home() / ".local/share/atum-agent-jobs/tools",
]
TOOLS = next((path for path in _candidates if path and (path / "review_cli.py").is_file()), None)
if TOOLS is None:
    raise SystemExit("agent-jobs tools not found; set AGENT_JOB_TOOLS_DIR to the repository tools directory")
sys.path.insert(0, str(TOOLS))

from review_cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
