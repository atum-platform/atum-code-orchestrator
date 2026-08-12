#!/usr/bin/env python3
"""Shared safety and lifecycle core for cross-agent read-only reviews."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any

from agent_job_client import cancel, inbox, list_jobs, read, route_decide, submit
from agent_job_policy import configured_allowed_roots, SENSITIVE_PATH_PARTS


MAX_CONTEXT_FILE_BYTES = 64_000
MAX_GIT_CONTEXT_BYTES = 256_000
MAX_PROMPT_BYTES = 4 * 1024 * 1024
MAX_WAIT_SECONDS = 60
PROVIDERS = {"claude", "kimi", "codex"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
GIT_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@{}~+-]{0,199}$")
SECRET_FILE_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*",
    "id_ed25519*", "credentials*.json", "service-account*.json", "*.kdbx",
    "*.p8", ".netrc", "*.jks", "*.keystore",
)
SENSITIVE_CONTENT_PATTERNS = (
    re.compile(r"(?im)(?P<prefix>\b(?:api[_-]?key|token|secret|password|passwd|private[_-]?key)\b\s*[:=]\s*)[^\s,'\"]+"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:sk|rk|pk)-(?:live|test)?-?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL),
)


def _configured_roots() -> list[Path]:
    return configured_allowed_roots()


def _within(path: Path, roots: list[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def safe_workdir(value: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ValueError("Workdir must be an absolute path")
    path = candidate.resolve()
    if not path.is_dir():
        raise ValueError(f"Review workdir does not exist or is not a directory: {path}")
    roots = [root for root in _configured_roots() if root.exists()]
    if not roots:
        raise ValueError("No configured review workspace roots exist; refusing to run fail-open")
    if not _within(path, roots):
        raise ValueError(f"Refusing review outside configured workspaces: {path}")
    if any(part.lower() in SENSITIVE_PATH_PARTS for part in path.parts):
        raise ValueError(f"Refusing review inside a credential or private-data store: {path}")
    return path


def safe_context_path(value: str, workdir: Path) -> Path:
    raw = Path(value).expanduser()
    path = (workdir / raw).resolve() if not raw.is_absolute() else raw.resolve()
    roots = [root for root in _configured_roots() if root.exists()]
    if not roots:
        raise ValueError("No configured review workspace roots exist; refusing to run fail-open")
    if not _within(path, roots):
        raise ValueError(f"Refusing context outside configured workspaces: {path}")
    if path != workdir and workdir not in path.parents:
        raise ValueError(f"Context files must be inside the selected workdir: {path}")
    if any(part.lower() in SENSITIVE_PATH_PARTS for part in path.relative_to(workdir).parts):
        raise ValueError(f"Refusing context inside a credential or private-data store: {path}")
    return path


def is_secret_path(path: Path) -> bool:
    return (
        any(part.lower() in SENSITIVE_PATH_PARTS for part in path.parts)
        or any(fnmatch.fnmatch(path.name.lower(), pattern.lower()) for pattern in SECRET_FILE_PATTERNS)
    )


def redact(text: str) -> tuple[str, int]:
    count = 0
    for pattern in SENSITIVE_CONTENT_PATTERNS:
        def replace(match: re.Match[str]) -> str:
            nonlocal count
            count += 1
            prefix = match.groupdict().get("prefix")
            return f"{prefix}[REDACTED]" if prefix is not None else "[REDACTED]"

        text = pattern.sub(replace, text)
    return text, count


def _run_git(cwd: Path, args: list[str]) -> tuple[bool, str]:
    env = {
        key: os.environ[key]
        for key in ("HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR")
        if os.environ.get(key)
    }
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_EXTERNAL_DIFF": "", "GIT_PAGER": "cat"})
    try:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False, ""
    return result.returncode == 0, result.stdout.decode("utf-8", errors="replace")


def git_context(workdir: Path, base_ref: str = "HEAD") -> str:
    workdir = workdir.resolve()
    if not GIT_REF_PATTERN.fullmatch(base_ref):
        raise ValueError("Git base contains unsupported characters")
    root_ok, root_text = _run_git(workdir, ["rev-parse", "--show-toplevel"])
    if not root_ok or not root_text.strip():
        return ""
    repo_root = Path(root_text.strip()).resolve()
    ref_ok, _ = _run_git(repo_root, ["rev-parse", "--verify", "--end-of-options", f"{base_ref}^{{commit}}"])
    if not ref_ok:
        raise ValueError(f"Git base does not resolve to a commit: {base_ref}")

    chunks: list[str] = []
    status_ok, status = _run_git(workdir, ["status", "--short", "--", "."])
    if status_ok and status.strip():
        chunks.append(f"--- git status (selected workdir) ---\n{status.strip()}")
    names_ok, raw_names = _run_git(repo_root, ["diff", "--name-only", "-z", base_ref])
    safe_names: list[str] = []
    secret_omitted = 0
    scope_omitted = 0
    if names_ok:
        for name in raw_names.split("\0"):
            if not name:
                continue
            path = (repo_root / name).resolve()
            if path != workdir and workdir not in path.parents:
                scope_omitted += 1
            elif is_secret_path(path):
                secret_omitted += 1
            else:
                safe_names.append(name)
    for index in range(0, min(len(safe_names), 500), 100):
        batch = safe_names[index:index + 100]
        ok, output = _run_git(repo_root, ["diff", "--no-ext-diff", "--no-color", base_ref, "--", *batch])
        if ok and output.strip():
            chunks.append(f"--- git diff ({index + 1}-{index + len(batch)}) ---\n{output.strip()}")
    untracked_ok, raw_untracked = _run_git(
        workdir, ["ls-files", "--others", "--exclude-standard", "-z", "--", "."]
    )
    untracked_included = 0
    if untracked_ok:
        for name in raw_untracked.split("\0"):
            if not name:
                continue
            raw_path = workdir / name
            if raw_path.is_symlink():
                scope_omitted += 1
                continue
            path = raw_path.resolve()
            if path != workdir and workdir not in path.parents:
                scope_omitted += 1
                continue
            if is_secret_path(path):
                secret_omitted += 1
                continue
            if not path.is_file():
                continue
            data = path.read_bytes()[:MAX_CONTEXT_FILE_BYTES]
            if b"\0" in data:
                chunks.append(f"[omitted binary untracked file: {path.relative_to(repo_root)}]")
                continue
            body = data.decode("utf-8", errors="replace")
            if path.stat().st_size > MAX_CONTEXT_FILE_BYTES:
                body += "\n[truncated]"
            chunks.append(f"--- untracked file: {path.relative_to(repo_root)} ---\n{body}")
            untracked_included += 1
            if sum(len(chunk.encode("utf-8")) for chunk in chunks) >= MAX_GIT_CONTEXT_BYTES:
                chunks.append("[stopped collecting untracked files at the Git-context byte budget]")
                break
            if untracked_included >= 100:
                chunks.append("[omitted additional untracked files after the 100-file limit]")
                break
    if secret_omitted:
        chunks.append(f"[omitted {secret_omitted} secret-like changed path(s)]")
    if scope_omitted:
        chunks.append(f"[omitted {scope_omitted} changed path(s) outside the selected workdir]")
    if len(safe_names) > 500:
        chunks.append(f"[omitted {len(safe_names) - 500} additional changed path(s)]")
    joined, redactions = redact("\n\n".join(chunks))
    if redactions:
        joined += f"\n[redacted {redactions} secret-like value(s) from Git context]"
    encoded = joined.encode("utf-8")
    if len(encoded) > MAX_GIT_CONTEXT_BYTES:
        joined = encoded[:MAX_GIT_CONTEXT_BYTES].decode("utf-8", errors="replace") + "\n[truncated]"
    return joined


def context_files_text(context_files: list[str] | None, workdir: Path) -> str:
    if not context_files:
        return ""
    sections = ["Context files supplied by caller:"]
    for value in context_files:
        if not str(value).strip():
            continue
        path = safe_context_path(str(value), workdir)
        if is_secret_path(path):
            raise ValueError(f"Refusing secret-like context file: {path}")
        sections.append(f"\n--- {path} ---")
        if not path.exists():
            sections.append("[missing]")
        elif path.is_dir():
            sections.append("[directory supplied; inspect with read-only tools if needed]")
        else:
            data = path.read_bytes()[:MAX_CONTEXT_FILE_BYTES]
            text, redactions = redact(data.decode("utf-8", errors="replace"))
            if path.stat().st_size > MAX_CONTEXT_FILE_BYTES:
                text += "\n[truncated]"
            if redactions:
                text += f"\n[redacted {redactions} secret-like value(s)]"
            sections.append(text)
    return "\n".join(sections).strip()


def build_review_prompt(
    provider: str,
    instructions: str,
    *,
    workdir: Path,
    context_git_diff: bool = False,
    context_git_base: str = "HEAD",
    context_files: list[str] | None = None,
    context_text: str = "",
    expected_output: str = "",
) -> str:
    if provider not in PROVIDERS:
        raise ValueError(f"Unsupported review provider: {provider}")
    if not instructions.strip():
        raise ValueError("instructions are required")
    clean_instructions, instruction_redactions = redact(instructions.strip())
    parts = [
        f"You are {provider.title()} acting as an independent read-only specialist.",
        "Return advice directly. Do not edit files, execute mutating commands, send messages, or change external systems.",
        "Use read-only inspection only. Keep findings evidence-backed, decision-oriented, and actionable.",
        "", f"Task:\n{clean_instructions}",
    ]
    if instruction_redactions:
        parts.append(f"[redacted {instruction_redactions} secret-like value(s) from instructions]")
    if expected_output.strip():
        clean_expected, redactions = redact(expected_output.strip())
        parts.extend(["", f"Expected output shape:\n{clean_expected}"])
        if redactions:
            parts.append(f"[redacted {redactions} secret-like value(s) from expected output]")
    if context_text.strip():
        clean_context, redactions = redact(context_text.strip())
        parts.extend(["", f"Additional context:\n{clean_context}"])
        if redactions:
            parts.append(f"[redacted {redactions} secret-like value(s) from additional context]")
    if context_git_diff:
        material = git_context(workdir, context_git_base)
        if material:
            parts.extend(["", f"Current Git context from {context_git_base}:\n{material}"])
    material = context_files_text(context_files, workdir)
    if material:
        parts.extend(["", material])
    prompt = "\n".join(parts).strip()
    size = len(prompt.encode("utf-8"))
    if size > MAX_PROMPT_BYTES:
        raise ValueError(f"Review prompt is too large ({size} bytes); maximum is {MAX_PROMPT_BYTES}")
    return prompt


def job_submit(
    *,
    provider: str,
    instructions: str,
    workdir: str,
    model: str = "",
    context_git_diff: bool = False,
    context_git_base: str = "HEAD",
    context_files: list[str] | None = None,
    context_text: str = "",
    expected_output: str = "",
    timeout_seconds: int = 1800,
    max_turns: int = 0,
    idempotency_key: str = "",
    label: str = "",
    owner: str = "",
) -> dict[str, Any]:
    cwd = safe_workdir(workdir)
    prompt = build_review_prompt(
        provider, instructions, workdir=cwd, context_git_diff=context_git_diff,
        context_git_base=context_git_base, context_files=context_files,
        context_text=context_text, expected_output=expected_output,
    )
    effective_owner = ":".join(part for part in (owner.strip(), label.strip()) if part)[:200]
    return submit(
        provider=provider, model=model, mode="readonly", workdir=str(cwd), prompt=prompt,
        timeout_seconds=timeout_seconds, max_turns=max_turns, owner=effective_owner,
        idempotency_key=idempotency_key,
    )


def routing_decide(**intent: Any) -> dict[str, Any]:
    return route_decide(**intent)


def job_read(
    job_id: str,
    cursor: int = 0,
    max_bytes: int = 64_000,
    wait_seconds: int = 0,
    event_cursor: int | None = None,
) -> dict[str, Any]:
    wait = max(0, min(int(wait_seconds), MAX_WAIT_SECONDS))
    return read(
        job_id, cursor=cursor, event_cursor=event_cursor,
        max_bytes=max_bytes, wait_seconds=wait,
    )


def job_list(status: str = "", limit: int = 50, owner: str = "") -> dict[str, Any]:
    return list_jobs(status=status, limit=limit, owner=owner)


def job_cancel(job_id: str) -> dict[str, Any]:
    return cancel(job_id)


def job_inbox(
    owner: str, limit: int = 20, ack_delivery_ids: list[str] | None = None
) -> dict[str, Any]:
    return inbox(owner=owner, limit=limit, ack_delivery_ids=ack_delivery_ids)
