from __future__ import annotations

from pathlib import Path
import sys
import unittest


TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

from agent_routing_policy import decide  # noqa: E402


class AgentRoutingPolicyTest(unittest.TestCase):
    def intent(self, caller: str, capability: str) -> dict[str, object]:
        surfaces = {
            "codex": "codex", "claude": "claude-code",
            "kimi": "kimi-code", "hermes": "hermes",
        }
        return {
            "protocol_version": 1,
            "caller_provider": caller,
            "surface": surfaces[caller],
            "capability": capability,
            "complexity": "standard",
            "risk": "medium",
            "scope": "single_module",
            "duration": "medium",
            "durability": "durable",
            "parallelizable": False,
            "surface_capabilities": {},
        }

    def test_current_review_table_is_centralized(self) -> None:
        cases = {
            ("codex", "code_review"): ("kimi", "claude"),
            ("codex", "planning"): ("claude", "kimi"),
            ("hermes", "code_review"): ("kimi", "claude"),
            ("claude", "planning"): ("codex", "kimi"),
            ("kimi", "code_review"): ("codex", "claude"),
            ("kimi", "research"): ("claude", "codex"),
        }
        for (caller, capability), expected in cases.items():
            with self.subTest(caller=caller, capability=capability):
                decision = decide(self.intent(caller, capability))
                self.assertEqual("agent_jobs", decision["lane"])
                self.assertEqual(expected, (
                    decision["provider"], decision["fallback_provider"]
                ))
                self.assertFalse(decision["enforced"])
                self.assertEqual("shadow", decision["mode"])

    def test_current_policy_does_not_automatically_delegate_implementation(self) -> None:
        decision = decide(self.intent("codex", "implementation"))
        self.assertEqual("direct", decision["lane"])
        self.assertEqual("", decision["provider"])

    def test_explicit_provider_wins_without_recursive_delegation(self) -> None:
        intent = self.intent("codex", "planning")
        intent["explicit_provider"] = "kimi"
        intent["explicit_model"] = "kimi-code/k3"
        decision = decide(intent)
        self.assertEqual("kimi", decision["provider"])
        self.assertEqual("kimi-code/k3", decision["model_alias"])

        intent["explicit_provider"] = "codex"
        decision = decide(intent)
        self.assertEqual("direct", decision["lane"])
        self.assertEqual("", decision["provider"])

    def test_protocol_and_surface_capability_validation_fail_closed(self) -> None:
        intent = self.intent("codex", "planning")
        intent["protocol_version"] = 2
        with self.assertRaisesRegex(ValueError, "protocol version"):
            decide(intent)
        intent["protocol_version"] = 1.9
        with self.assertRaisesRegex(ValueError, "protocol version"):
            decide(intent)
        intent["protocol_version"] = 1
        intent["surface_capabilities"] = {"native_subagents": "yes"}
        with self.assertRaisesRegex(ValueError, "boolean"):
            decide(intent)

    def test_recursive_direct_route_has_no_model_alias(self) -> None:
        intent = self.intent("codex", "planning")
        intent.update(explicit_provider="codex", explicit_model="gpt-test")
        decision = decide(intent)
        self.assertEqual("direct", decision["lane"])
        self.assertEqual("", decision["model_alias"])
