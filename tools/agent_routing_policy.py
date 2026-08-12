"""Versioned routing policy for cross-agent and native-worker work."""

from __future__ import annotations

from typing import Any


PROTOCOL_VERSION = 1
POLICY_VERSION = "2026-08-12.3"
ROUTING_MODES = {"shadow", "codex_canary"}

PROVIDERS = {"codex", "claude", "kimi", "hermes"}
SURFACES = {"codex", "claude-code", "claude-desktop", "kimi-code", "hermes"}
CAPABILITIES = {
    "implementation", "code_review", "planning", "architecture", "design",
    "product", "copywriting", "research", "exploration", "tests",
}
COMPLEXITIES = {"trivial", "focused", "standard", "deep"}
RISKS = {"low", "medium", "high"}
SCOPES = {"local", "single_module", "cross_module", "repo"}
DURATIONS = {"short", "medium", "long"}
DURABILITIES = {"session", "durable"}
TARGET_PROVIDERS = {"codex", "claude", "kimi"}
MAX_INTENT_BYTES = 16 * 1024

MODEL_ALIASES = {
    "codex": "codex_standard",
    "claude": "claude_deep",
    "kimi": "kimi_standard",
}

NATIVE_CODEX_CAPABILITIES = {"implementation", "exploration", "tests"}


def _required_enum(intent: dict[str, Any], key: str, allowed: set[str]) -> str:
    value = str(intent.get(key) or "").strip().lower()
    if value not in allowed:
        raise ValueError(f"Unsupported {key}: {value or '<empty>'}")
    return value


def _optional_enum(intent: dict[str, Any], key: str, allowed: set[str], default: str) -> str:
    value = str(intent.get(key) or default).strip().lower()
    if value not in allowed:
        raise ValueError(f"Unsupported {key}: {value}")
    return value


def _default_targets(caller: str, capability: str) -> tuple[str, str]:
    code_review = capability == "code_review"
    if caller in {"codex", "hermes"}:
        return ("kimi", "claude") if code_review else ("claude", "kimi")
    if caller == "claude":
        return "codex", "kimi"
    return ("codex", "claude") if code_review else ("claude", "codex")


def normalize_intent(intent: dict[str, Any]) -> dict[str, Any]:
    """Validate and return only canonical protocol fields."""
    version = intent.get("protocol_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError(
            f"Unsupported routing protocol version: {version!r}; expected {PROTOCOL_VERSION}"
        )
    if version != PROTOCOL_VERSION:
        raise ValueError(
            f"Unsupported routing protocol version: {version}; expected {PROTOCOL_VERSION}"
        )
    caller = _required_enum(intent, "caller_provider", PROVIDERS)
    surface = _required_enum(intent, "surface", SURFACES)
    capability = _required_enum(intent, "capability", CAPABILITIES)
    complexity = _optional_enum(intent, "complexity", COMPLEXITIES, "standard")
    risk = _optional_enum(intent, "risk", RISKS, "medium")
    scope = _optional_enum(intent, "scope", SCOPES, "single_module")
    duration = _optional_enum(intent, "duration", DURATIONS, "medium")
    durability = _optional_enum(intent, "durability", DURABILITIES, "session")
    if not isinstance(intent.get("parallelizable", False), bool):
        raise ValueError("parallelizable must be a boolean")
    capabilities = intent.get("surface_capabilities") or {}
    if not isinstance(capabilities, dict) or any(
        not isinstance(key, str) or not isinstance(value, bool)
        for key, value in capabilities.items()
    ):
        raise ValueError("surface_capabilities must be an object of boolean values")

    explicit_provider = str(intent.get("explicit_provider") or "").strip().lower()
    explicit_model = str(intent.get("explicit_model") or "").strip()
    session_id = str(intent.get("session_id") or "").strip()
    if explicit_provider and explicit_provider not in TARGET_PROVIDERS:
        raise ValueError(f"Unsupported explicit_provider: {explicit_provider}")
    if len(explicit_model) > 200:
        raise ValueError("explicit_model is too long")
    if len(session_id) > 200:
        raise ValueError("session_id is too long")

    return {
        "protocol_version": version,
        "caller_provider": caller,
        "surface": surface,
        "capability": capability,
        "complexity": complexity,
        "risk": risk,
        "scope": scope,
        "duration": duration,
        "durability": durability,
        "parallelizable": intent.get("parallelizable", False),
        "surface_capabilities": dict(sorted(capabilities.items())),
        "explicit_provider": explicit_provider,
        "explicit_model": explicit_model,
        "session_id": session_id,
    }


def decide(intent: dict[str, Any], routing_mode: str = "shadow") -> dict[str, Any]:
    """Return a centralized decision; persistence performs stateful admission."""
    intent = normalize_intent(intent)
    if routing_mode not in ROUTING_MODES:
        raise ValueError(f"Unsupported routing mode: {routing_mode}")
    caller = intent["caller_provider"]
    surface = intent["surface"]
    capability = intent["capability"]
    explicit_provider = intent["explicit_provider"]
    explicit_model = intent["explicit_model"]

    canary = routing_mode == "codex_canary" and caller == "codex" and surface == "codex"
    mode = "codex_canary" if canary else "shadow"
    enforced = canary
    reasons = (
        ["Codex canary decision is authoritative for this cooperating caller"]
        if canary else
        ["shadow decision only; existing caller behavior remains authoritative"]
    )
    if explicit_provider:
        if explicit_provider == caller:
            lane = "direct"
            provider = ""
            fallback_provider = ""
            reasons.append("recursive delegation to the calling provider is not allowed")
        else:
            lane = "agent_jobs"
            provider = explicit_provider
            fallback_provider = ""
            reasons.append("explicit provider request overrides default routing")
    elif (
        canary
        and capability in NATIVE_CODEX_CAPABILITIES
        and intent["complexity"] in {"trivial", "focused"}
        and intent["risk"] in {"low", "medium"}
        and intent["scope"] in {"local", "single_module"}
        and intent["duration"] in {"short", "medium"}
        and intent["durability"] == "session"
        and intent["surface_capabilities"].get("native_subagents", False)
        and intent["session_id"]
    ):
        lane = "native_subagent"
        provider = "codex"
        fallback_provider = ""
        reasons.append("focused session-scoped work fits a Codex native worker")
    elif capability in {
        "code_review", "planning", "architecture", "design", "product",
        "copywriting", "research",
    }:
        lane = "agent_jobs"
        provider, fallback_provider = _default_targets(caller, capability)
        reasons.append("centralized copy of the current independent-specialist routing table")
    else:
        lane = "direct"
        provider = ""
        fallback_provider = ""
        reasons.append("current policy does not automatically delegate this capability")

    return {
        "protocol_version": PROTOCOL_VERSION,
        "policy_version": POLICY_VERSION,
        "mode": mode,
        "enforced": enforced,
        "surface": surface,
        "lane": lane,
        "provider": provider,
        "model_alias": (
            "" if lane == "direct" else
            "codex_fast" if lane == "native_subagent" else
            explicit_model or MODEL_ALIASES.get(provider, "")
        ),
        "worker_profile": "spark-worker" if lane == "native_subagent" else "",
        "fallback_provider": fallback_provider,
        "fallback_model_alias": MODEL_ALIASES.get(fallback_provider, ""),
        "reasons": reasons,
        "expires_at": None,
        "reservation_status": "none",
    }
