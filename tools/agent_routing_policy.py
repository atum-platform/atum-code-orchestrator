"""Versioned routing policy for cross-agent and native-worker work."""

from __future__ import annotations

from typing import Any


PROTOCOL_VERSION = 2
SUPPORTED_PROTOCOL_VERSIONS = {1, 2}
POLICY_VERSION = "2026-08-13.2"
CAPABILITY_MATRIX_VERSION = "2026-08-13.1"
ROUTING_MODES = {"shadow", "codex_canary", "surface_canary"}

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
ESCALATION_REASONS = {
    "provider_failure", "rate_limit", "unusable_output", "scope_growth",
    "capability_mismatch",
}
MAX_ESCALATION_EVIDENCE_CHARS = 2_000

LEGACY_MODEL_ALIASES = {
    "codex": "codex_standard",
    "claude": "claude_deep",
    "kimi": "kimi_standard",
}

PROVIDER_CAPABILITY_MATRIX = {
    "codex": {
        "deep_model": "gpt-5.6-sol",
        "standard_model": "gpt-5.6-sol",
        "fast_model": "gpt-5.3-codex-spark",
        "strengths": ["implementation", "tests", "exploration", "code_review"],
    },
    "claude": {
        "deep_model": "opus",
        "standard_model": "sonnet",
        "explicit_only_models": ["fable"],
        "strengths": ["planning", "architecture", "design", "product", "copywriting", "research", "code_review"],
    },
    "kimi": {
        "deep_model": "kimi-code/k3",
        "standard_model": "kimi-code/kimi-for-coding",
        "fast_model": "kimi-code/kimi-for-coding-highspeed",
        "strengths": ["code_review", "implementation", "tests", "exploration"],
    },
}

SURFACE_CAPABILITY_MATRIX = {
    "codex": {"durable_agent_jobs", "native_subagents"},
    "claude-code": {"durable_agent_jobs"},
    "claude-desktop": {"durable_agent_jobs"},
    "kimi-code": {"durable_agent_jobs"},
    "hermes": {"durable_agent_jobs"},
}
CALLER_SURFACES = {
    "codex": {"codex"},
    "claude": {"claude-code", "claude-desktop"},
    "kimi": {"kimi-code"},
    "hermes": {"hermes"},
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
    if version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise ValueError(
            f"Unsupported routing protocol version: {version}; supported versions are "
            f"{sorted(SUPPORTED_PROTOCOL_VERSIONS)}"
        )
    caller = _required_enum(intent, "caller_provider", PROVIDERS)
    surface = _required_enum(intent, "surface", SURFACES)
    if surface not in CALLER_SURFACES[caller]:
        raise ValueError(f"Surface {surface} does not belong to caller {caller}")
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
    previous_decision_id = str(intent.get("previous_decision_id") or "").strip()
    escalation_reason = str(intent.get("escalation_reason") or "").strip().lower()
    escalation_evidence = str(intent.get("escalation_evidence") or "").strip()
    if explicit_provider and explicit_provider not in TARGET_PROVIDERS:
        raise ValueError(f"Unsupported explicit_provider: {explicit_provider}")
    if len(explicit_model) > 200:
        raise ValueError("explicit_model is too long")
    if len(session_id) > 200:
        raise ValueError("session_id is too long")
    if len(previous_decision_id) > 200:
        raise ValueError("previous_decision_id is too long")
    if previous_decision_id:
        if version != 2:
            raise ValueError("Escalation requires routing protocol version 2")
        if not session_id:
            raise ValueError("Escalation requires session_id")
        if escalation_reason not in ESCALATION_REASONS:
            raise ValueError(f"Unsupported escalation_reason: {escalation_reason or '<empty>'}")
        if not escalation_evidence or len(escalation_evidence) > MAX_ESCALATION_EVIDENCE_CHARS:
            raise ValueError(
                f"escalation_evidence must contain 1 to {MAX_ESCALATION_EVIDENCE_CHARS} characters"
            )
    elif escalation_reason or escalation_evidence:
        raise ValueError("Escalation reason/evidence require previous_decision_id")

    declared_capabilities = dict(sorted(capabilities.items()))
    if version == 1:
        effective_capabilities = {
            "durable_agent_jobs": True,
            "native_subagents": bool(declared_capabilities.get("native_subagents", False)),
        }
    else:
        allowed = SURFACE_CAPABILITY_MATRIX[surface]
        effective_capabilities = {
            name: bool(declared_capabilities.get(name, False)) and name in allowed
            for name in ("durable_agent_jobs", "native_subagents")
        }

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
        "surface_capabilities": declared_capabilities,
        "effective_surface_capabilities": effective_capabilities,
        "explicit_provider": explicit_provider,
        "explicit_model": explicit_model,
        "session_id": session_id,
        "previous_decision_id": previous_decision_id,
        "escalation_reason": escalation_reason,
        "escalation_evidence": escalation_evidence,
    }


def apply_one_hop_escalation(
    decision: dict[str, Any], previous: dict[str, Any], health: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Exclude the parent provider and return one terminal fallback decision."""
    escalated = dict(decision)
    previous_provider = str(previous.get("provider") or "")
    if escalated["lane"] == "native_subagent" and escalated["provider"] == previous_provider:
        escalated.update(lane="direct", provider="", model_alias="", worker_profile="")
    elif escalated["lane"] == "agent_jobs" and escalated["provider"] == previous_provider:
        fallback = str(escalated.get("fallback_provider") or "")
        if fallback and health.get(fallback, {}).get("state") != "rate_limited":
            escalated.update(
                provider=fallback,
                model_alias=str(escalated.get("fallback_model_alias") or ""),
            )
        else:
            escalated.update(lane="direct", provider="", model_alias="", worker_profile="")
    escalated.update(fallback_provider="", fallback_model_alias="", escalation_hop=1)
    escalated["reasons"] = [
        *escalated["reasons"],
        "one-hop escalation excludes the parent decision provider",
    ]
    return escalated


def _model_alias(provider: str, intent: dict[str, Any]) -> str:
    if intent["protocol_version"] == 1:
        return LEGACY_MODEL_ALIASES.get(provider, "")
    matrix = PROVIDER_CAPABILITY_MATRIX[provider]
    if provider == "claude":
        return matrix["deep_model"] if (
            intent["capability"] in {
                "code_review", "planning", "architecture", "design", "product",
                "copywriting", "research",
            }
            or intent["complexity"] == "deep"
        ) else matrix["standard_model"]
    if provider == "kimi":
        if intent["capability"] == "code_review" or intent["complexity"] in {"standard", "deep"}:
            return matrix["deep_model"]
        return matrix["fast_model"] if intent["complexity"] == "trivial" else matrix["standard_model"]
    return matrix["deep_model"] if intent["complexity"] == "deep" else matrix["standard_model"]


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

    codex_canary = routing_mode == "codex_canary" and caller == "codex" and surface == "codex"
    surface_canary = (
        routing_mode == "surface_canary"
        and intent["protocol_version"] == 2
        and surface in {"codex", "claude-code", "claude-desktop", "kimi-code"}
    )
    canary = codex_canary or surface_canary
    mode = routing_mode if canary else "shadow"
    enforced = canary
    reasons = (
        [f"{mode} decision is authoritative for this cooperating caller"]
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
        and intent["effective_surface_capabilities"].get("native_subagents", False)
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

    degraded_from_lane = ""
    if (
        intent["protocol_version"] == 2
        and lane == "agent_jobs"
        and not intent["effective_surface_capabilities"].get("durable_agent_jobs", False)
    ):
        degraded_from_lane = lane
        lane = "direct"
        provider = ""
        fallback_provider = ""
        reasons.append("client cannot execute durable agent jobs; degraded to direct")

    return {
        "protocol_version": intent["protocol_version"],
        "latest_protocol_version": PROTOCOL_VERSION,
        "policy_version": POLICY_VERSION,
        "capability_matrix_version": CAPABILITY_MATRIX_VERSION,
        "mode": mode,
        "enforced": enforced,
        "surface": surface,
        "lane": lane,
        "provider": provider,
        "model_alias": (
            "" if lane == "direct" else
            (
                "codex_fast" if intent["protocol_version"] == 1
                else PROVIDER_CAPABILITY_MATRIX["codex"]["fast_model"]
            ) if lane == "native_subagent" else
            explicit_model or _model_alias(provider, intent)
        ),
        "worker_profile": "spark-worker" if lane == "native_subagent" else "",
        "fallback_provider": fallback_provider,
        "fallback_model_alias": _model_alias(fallback_provider, intent) if fallback_provider else "",
        "effective_surface_capabilities": intent["effective_surface_capabilities"],
        "degraded_from_lane": degraded_from_lane,
        "reasons": reasons,
        "expires_at": None,
        "reservation_status": "none",
    }
