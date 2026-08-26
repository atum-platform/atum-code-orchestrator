#!/usr/bin/env python3
"""Shared machine policy for all agent-job bindings and the supervisor."""

from __future__ import annotations

import os
from pathlib import Path


SENSITIVE_PATH_PARTS = {
    ".ssh", ".aws", ".azure", ".gnupg", "keychains", "credentials", "secrets",
}


def default_allowed_roots(home: Path | None = None) -> list[Path]:
    home = (home or Path.home()).expanduser().resolve()
    return [
        home / "Documents",
        home / "Projects",
        home / "projects",
        Path("/Users/Shared"),
        home / ".local/share/atum-agent-jobs",
        home / ".codex/worktrees",
        home / ".atum/worktrees",
    ]


def configured_allowed_roots() -> list[Path]:
    raw = os.environ.get("AGENT_JOB_ALLOWED_ROOTS", "").strip()
    if not raw:
        raw = os.pathsep.join(str(path) for path in default_allowed_roots())
    return [Path(value).expanduser().resolve() for value in raw.split(os.pathsep) if value.strip()]


def allowed_roots_value(home: Path | None = None) -> str:
    return os.pathsep.join(str(path) for path in default_allowed_roots(home))
