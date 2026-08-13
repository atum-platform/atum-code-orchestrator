"""Local provider quota telemetry and deterministic pressure evaluation."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any


PROVIDERS = ("claude", "codex", "kimi")
DEFAULT_HISTORY_DIR = Path(
    "~/Library/Application Support/com.steipete.codexbar/history"
).expanduser()
PRESSURE_ENTER = 85.0
PRESSURE_EXIT = 70.0
EXHAUSTED_ENTER = 98.0
EXHAUSTED_EXIT = 95.0
DEFAULT_STALE_SECONDS = 2 * 3600
DEFAULT_COOLDOWN_SECONDS = 15 * 60
RATE_LIMIT_PATTERNS = {
    "claude": re.compile(
        r"(?:rate[ -]?limit(?:ed| reached| exceeded)?|usage[ -]?limit(?: reached| exceeded)|"
        r"credit balance is too low|too many requests|\b429\b)", re.IGNORECASE,
    ),
    "codex": re.compile(
        r"(?:rate[ -]?limit(?:ed| reached| exceeded)?|usage[ -]?limit(?: reached| exceeded)|"
        r"quota (?:exceeded|exhausted|reached)|too many requests|\b429\b)", re.IGNORECASE,
    ),
    "kimi": re.compile(
        r"(?:rate[ -]?limit(?:ed| reached| exceeded)?|usage[ -]?limit(?: reached| exceeded)|"
        r"quota (?:exceeded|exhausted|reached)|out of usage|too many requests|\b429\b)",
        re.IGNORECASE,
    ),
}
RETRY_AFTER_PATTERN = re.compile(
    r"(?:try again|retry|resets?)(?:\s+at|\s+after|\s+in)?\s+"
    r"(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>seconds?|secs?|minutes?|mins?|hours?|hrs?)",
    re.IGNORECASE,
)


def _epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _latest_windows(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    accounts = payload.get("accounts")
    if not isinstance(accounts, dict):
        return []
    preferred = str(payload.get("preferredAccountKey") or "")
    windows = accounts.get(preferred)
    if not isinstance(windows, list) and accounts:
        windows = next(iter(accounts.values()))
    return windows if isinstance(windows, list) else []


def read_codexbar_history(provider: str, now: float) -> dict[str, Any] | None:
    history_dir = Path(
        os.environ.get("AGENT_JOB_QUOTA_HISTORY_DIR", str(DEFAULT_HISTORY_DIR))
    ).expanduser()
    windows = _latest_windows(history_dir / f"{provider}.json")
    samples: list[dict[str, Any]] = []
    for window in windows:
        if not isinstance(window, dict):
            continue
        entries = window.get("entries")
        if not isinstance(entries, list) or not entries:
            continue
        valid = [entry for entry in entries if isinstance(entry, dict) and _epoch(entry.get("capturedAt"))]
        if not valid:
            continue
        entry = max(valid, key=lambda item: _epoch(item.get("capturedAt")) or 0)
        captured_at = _epoch(entry.get("capturedAt"))
        used = entry.get("usedPercent")
        if captured_at is None or isinstance(used, bool) or not isinstance(used, (int, float)):
            continue
        reset_at = _epoch(entry.get("resetsAt"))
        window_minutes = window.get("windowMinutes")
        projected = float(used)
        if (
            reset_at is not None
            and isinstance(window_minutes, (int, float))
            and window_minutes > 0
        ):
            started_at = reset_at - float(window_minutes) * 60
            elapsed_fraction = (captured_at - started_at) / (float(window_minutes) * 60)
            if elapsed_fraction >= 0.05:
                projected = min(100.0, float(used) / min(1.0, elapsed_fraction))
        samples.append({
            "name": str(window.get("name") or "window")[:80],
            "used_percent": max(0.0, min(100.0, float(used))),
            "projected_percent": max(0.0, min(100.0, projected)),
            "captured_at": captured_at,
            "resets_at": reset_at,
        })
    if not samples:
        return None
    freshest = max(sample["captured_at"] for sample in samples)
    active = [sample for sample in samples if sample["resets_at"] is None or sample["resets_at"] > now]
    if not active:
        return {
            "provider": provider,
            "source": "codexbar",
            "captured_at": freshest,
            "resets_at": None,
            "pressure": None,
            "windows": [],
            "expired": True,
        }
    considered = active
    pressure = max(
        max(sample["used_percent"], sample["projected_percent"])
        for sample in considered
    )
    used_percent = max(sample["used_percent"] for sample in considered)
    resets = [sample["resets_at"] for sample in active if sample["resets_at"] is not None]
    return {
        "provider": provider,
        "source": "codexbar",
        "captured_at": freshest,
        "resets_at": min(resets) if resets else None,
        "pressure": pressure,
        "used_percent": used_percent,
        "windows": considered,
    }


def evaluate_health(
    provider: str,
    now: float,
    previous_state: str = "unknown",
    cooldown_until: float | None = None,
    stale_seconds: int = DEFAULT_STALE_SECONDS,
) -> dict[str, Any]:
    telemetry = read_codexbar_history(provider, now)
    if cooldown_until is not None and cooldown_until > now:
        return {
            "provider": provider,
            "state": "rate_limited",
            "pressure": 100.0,
            "source": "provider_failure",
            "captured_at": None if telemetry is None else telemetry["captured_at"],
            "resets_at": None if telemetry is None else telemetry["resets_at"],
            "cooldown_until": cooldown_until,
            "alert": "provider is cooling down after a canonical rate-limit failure",
        }
    if telemetry is None:
        return {
            "provider": provider,
            "state": "unknown",
            "pressure": None,
            "source": "none",
            "captured_at": None,
            "resets_at": None,
            "cooldown_until": None,
            "alert": "quota telemetry is unavailable; static routing remains in effect",
        }
    if telemetry.get("expired"):
        return {
            **telemetry,
            "state": "stale",
            "cooldown_until": None,
            "alert": "quota telemetry belongs to an expired window; static routing remains in effect",
        }
    age = max(0.0, now - float(telemetry["captured_at"]))
    if age > stale_seconds:
        return {
            **telemetry,
            "state": "stale",
            "cooldown_until": None,
            "alert": f"quota telemetry is stale by {int(age)} seconds; static routing remains in effect",
        }
    pressure = float(telemetry["pressure"])
    used_percent = float(telemetry["used_percent"])
    exhausted = used_percent >= EXHAUSTED_ENTER or (
        previous_state == "exhausted" and used_percent > EXHAUSTED_EXIT
    )
    pressured = pressure >= PRESSURE_ENTER or (
        previous_state in {"pressured", "rate_limited", "exhausted"}
        and pressure > PRESSURE_EXIT
    )
    return {
        **telemetry,
        "state": "exhausted" if exhausted else "pressured" if pressured else "available",
        "cooldown_until": None,
        "alert": "",
    }


def rate_limit_cooldown(
    provider: str, text: str, now: float,
    default_cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
) -> tuple[bool, float | None, str]:
    bounded = text[-16_000:]
    pattern = RATE_LIMIT_PATTERNS.get(provider)
    match = None if pattern is None else pattern.search(bounded)
    if not match:
        return False, None, ""
    nearby = bounded[max(0, match.start() - 500):min(len(bounded), match.end() + 500)]
    retry = RETRY_AFTER_PATTERN.search(nearby)
    if retry:
        amount = float(retry.group("amount"))
        unit = retry.group("unit").lower()
        multiplier = 1 if unit.startswith("sec") else 60 if unit.startswith("min") else 3600
        seconds = max(60, min(int(amount * multiplier), 7 * 24 * 3600))
        evidence = retry.group(0)[:300]
    else:
        seconds = default_cooldown_seconds
        evidence = match.group(0)[:300]
    return True, now + seconds, evidence


def rebalance_default_route(
    decision: dict[str, Any], health: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    result = dict(decision)
    if result.get("lane") != "agent_jobs" or not result.get("fallback_provider"):
        return result
    primary = health.get(str(result["provider"]), {})
    fallback = health.get(str(result["fallback_provider"]), {})
    primary_state = primary.get("state", "unknown")
    fallback_state = fallback.get("state", "unknown")
    if primary_state not in {"pressured", "rate_limited", "exhausted"}:
        if primary_state in {"stale", "unknown"} and primary.get("alert"):
            result["reasons"] = [*result["reasons"], str(primary["alert"])]
        return result
    if fallback_state in {"rate_limited", "exhausted"}:
        if primary_state == "exhausted":
            result.update(
                lane="direct", provider="", model_alias="",
                fallback_provider="", fallback_model_alias="",
            )
            result["reasons"] = [
                *result["reasons"],
                "both default providers are unavailable from quota exhaustion or cooldown; continuing directly",
            ]
            return result
        result["reasons"] = [
            *result["reasons"],
            f"{result['provider']} is {primary_state}, but fallback {result['fallback_provider']} is {fallback_state}",
        ]
        return result
    if fallback_state == "pressured" and primary_state == "pressured":
        primary_pressure = primary.get("pressure")
        fallback_pressure = fallback.get("pressure")
        if (
            not isinstance(primary_pressure, (int, float))
            or not isinstance(fallback_pressure, (int, float))
            or fallback_pressure >= primary_pressure
        ):
            result["reasons"] = [
                *result["reasons"],
                f"both default providers are pressured; keeping lower-pressure {result['provider']}",
            ]
            return result
    old_provider = str(result["provider"])
    old_model = str(result["model_alias"])
    result["provider"], result["fallback_provider"] = (
        result["fallback_provider"], old_provider
    )
    result["model_alias"], result["fallback_model_alias"] = (
        result["fallback_model_alias"], old_model
    )
    result["reasons"] = [
        *result["reasons"],
        f"quota broker moved default routing away from {old_provider} ({primary_state})",
    ]
    if fallback_state in {"stale", "unknown"}:
        result["reasons"].append(
            f"{result['provider']} has {fallback_state} quota telemetry; provider failures still trigger cooldown"
        )
    return result


def enforce_provider_availability(
    decision: dict[str, Any], health: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Prevent automatic escalated or native routes from selecting unavailable providers."""
    result = dict(decision)
    if result.get("lane") not in {"agent_jobs", "native_subagent"}:
        return result
    provider = str(result.get("provider") or "")
    state = health.get(provider, {}).get("state", "unknown")
    if state not in {"rate_limited", "exhausted"}:
        return result
    result.update(
        lane="direct", provider="", model_alias="", worker_profile="",
        fallback_provider="", fallback_model_alias="",
    )
    result["reasons"] = [
        *result["reasons"],
        f"quota enforcement rejected unavailable {provider} ({state}); continuing directly",
    ]
    return result
