from __future__ import annotations

from pathlib import Path
import sys
import unittest


TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

from agent_routing_policy import apply_one_hop_escalation, decide, normalize_intent  # noqa: E402


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
        intent["protocol_version"] = 3
        with self.assertRaisesRegex(ValueError, "protocol version"):
            decide(intent)
        intent["protocol_version"] = 1.9
        with self.assertRaisesRegex(ValueError, "protocol version"):
            decide(intent)
        intent["protocol_version"] = 1
        intent["surface_capabilities"] = {"native_subagents": "yes"}
        with self.assertRaisesRegex(ValueError, "boolean"):
            decide(intent)

    def test_escalation_intent_requires_v2_session_reason_and_evidence(self) -> None:
        intent = self.intent("codex", "planning")
        intent["previous_decision_id"] = "parent"
        intent["session_id"] = "task"
        intent["escalation_reason"] = "provider_failure"
        intent["escalation_evidence"] = "provider exited"
        with self.assertRaisesRegex(ValueError, "version 2"):
            normalize_intent(intent)

        intent["protocol_version"] = 2
        intent["escalation_reason"] = ""
        with self.assertRaisesRegex(ValueError, "escalation_reason"):
            normalize_intent(intent)
        intent["escalation_reason"] = "provider_failure"
        intent["escalation_evidence"] = ""
        with self.assertRaisesRegex(ValueError, "1 to 2000"):
            normalize_intent(intent)
        intent["escalation_evidence"] = "x" * 2001
        with self.assertRaisesRegex(ValueError, "1 to 2000"):
            normalize_intent(intent)

        orphan = self.intent("codex", "planning")
        orphan["protocol_version"] = 2
        orphan["escalation_reason"] = "provider_failure"
        orphan["escalation_evidence"] = "provider exited"
        with self.assertRaisesRegex(ValueError, "require previous_decision_id"):
            normalize_intent(orphan)

    def test_one_hop_escalation_excludes_parent_and_clears_fallback(self) -> None:
        decision = {
            "lane": "agent_jobs", "provider": "claude", "model_alias": "opus",
            "fallback_provider": "kimi", "fallback_model_alias": "kimi-code/k3",
            "worker_profile": "", "reasons": ["default"],
        }
        for health in ({}, {"kimi": {"state": "pressured"}},
                       {"kimi": {"state": "stale"}}, {"kimi": {"state": "unknown"}},
                       {"kimi": {"state": "available"}}):
            with self.subTest(health=health):
                escalated = apply_one_hop_escalation(
                    decision, {"provider": "claude"}, health,
                )
                self.assertEqual("kimi", escalated["provider"])
                self.assertEqual("kimi-code/k3", escalated["model_alias"])
                self.assertEqual("", escalated["fallback_provider"])
                self.assertEqual(1, escalated["escalation_hop"])

        unavailable = apply_one_hop_escalation(
            decision, {"provider": "claude"}, {"kimi": {"state": "rate_limited"}},
        )
        self.assertEqual("direct", unavailable["lane"])
        self.assertEqual("", unavailable["provider"])

    def test_explicit_escalation_target_still_excludes_parent_provider(self) -> None:
        intent = self.intent("codex", "planning")
        intent.update(
            protocol_version=2, session_id="task", previous_decision_id="parent",
            escalation_reason="provider_failure", escalation_evidence="provider exited",
            explicit_provider="claude", explicit_model="opus",
            surface_capabilities={"durable_agent_jobs": True},
        )
        same_provider = apply_one_hop_escalation(
            decide(intent, "surface_canary"), {"provider": "claude"}, {},
        )
        self.assertEqual("direct", same_provider["lane"])

        intent.update(explicit_provider="kimi", explicit_model="kimi-code/k3")
        different_provider = apply_one_hop_escalation(
            decide(intent, "surface_canary"), {"provider": "claude"}, {},
        )
        self.assertEqual("kimi", different_provider["provider"])

    def test_native_worker_escalation_degrades_to_direct(self) -> None:
        decision = {
            "lane": "native_subagent", "provider": "codex",
            "model_alias": "gpt-5.3-codex-spark", "worker_profile": "spark-worker",
            "fallback_provider": "", "fallback_model_alias": "",
            "reasons": ["focused native worker"],
        }
        escalated = apply_one_hop_escalation(
            decision, {"provider": "codex"}, {},
        )
        self.assertEqual("direct", escalated["lane"])
        self.assertEqual("", escalated["provider"])
        self.assertEqual("", escalated["model_alias"])
        self.assertEqual("", escalated["worker_profile"])
        self.assertEqual(1, escalated["escalation_hop"])

    def test_v2_selects_exact_claude_and_kimi_models(self) -> None:
        planning = self.intent("codex", "planning")
        planning.update(
            protocol_version=2,
            surface_capabilities={"durable_agent_jobs": True},
        )
        review = self.intent("codex", "code_review")
        review.update(
            protocol_version=2,
            surface_capabilities={"durable_agent_jobs": True},
        )

        planning_decision = decide(planning)
        review_decision = decide(review)

        self.assertEqual("opus", planning_decision["model_alias"])
        self.assertEqual("kimi-code/k3", planning_decision["fallback_model_alias"])
        self.assertEqual("kimi-code/k3", review_decision["model_alias"])
        self.assertEqual("opus", review_decision["fallback_model_alias"])

    def test_v2_degrades_when_surface_cannot_execute_selected_lane(self) -> None:
        intent = self.intent("claude", "planning")
        intent.update(protocol_version=2, surface_capabilities={})

        decision = decide(intent, "surface_canary")

        self.assertTrue(decision["enforced"])
        self.assertEqual("direct", decision["lane"])
        self.assertEqual("agent_jobs", decision["degraded_from_lane"])
        self.assertEqual("", decision["provider"])

    def test_surface_canary_enforces_v2_durable_route(self) -> None:
        intent = self.intent("claude", "planning")
        intent.update(
            protocol_version=2,
            surface_capabilities={"durable_agent_jobs": True},
        )

        decision = decide(intent, "surface_canary")

        self.assertTrue(decision["enforced"])
        self.assertEqual("surface_canary", decision["mode"])
        self.assertEqual("agent_jobs", decision["lane"])
        self.assertEqual("codex", decision["provider"])

    def test_surface_matrix_rejects_unsupported_native_claim(self) -> None:
        intent = self.intent("claude", "implementation")
        intent.update(
            protocol_version=2, complexity="focused", durability="session",
            session_id="task", surface_capabilities={
                "durable_agent_jobs": True, "native_subagents": True,
            },
        )

        decision = decide(intent, "surface_canary")

        self.assertFalse(decision["effective_surface_capabilities"]["native_subagents"])
        self.assertEqual("direct", decision["lane"])

    def test_surface_must_belong_to_caller(self) -> None:
        intent = self.intent("claude", "planning")
        intent["surface"] = "codex"
        with self.assertRaisesRegex(ValueError, "does not belong"):
            decide(intent)

    def test_v1_claude_remains_shadow_in_surface_canary(self) -> None:
        decision = decide(self.intent("claude", "planning"), "surface_canary")
        self.assertFalse(decision["enforced"])
        self.assertEqual("shadow", decision["mode"])

    def test_v1_codex_also_remains_shadow_in_surface_canary(self) -> None:
        decision = decide(self.intent("codex", "planning"), "surface_canary")
        self.assertFalse(decision["enforced"])
        self.assertEqual("shadow", decision["mode"])

    def test_v2_codex_target_uses_concrete_model(self) -> None:
        intent = self.intent("claude", "planning")
        intent.update(
            protocol_version=2,
            surface_capabilities={"durable_agent_jobs": True},
        )
        decision = decide(intent, "surface_canary")
        self.assertEqual("codex", decision["provider"])
        self.assertEqual("gpt-5.6-sol", decision["model_alias"])

    def test_recursive_direct_route_has_no_model_alias(self) -> None:
        intent = self.intent("codex", "planning")
        intent.update(explicit_provider="codex", explicit_model="gpt-test")
        decision = decide(intent)
        self.assertEqual("direct", decision["lane"])
        self.assertEqual("", decision["model_alias"])

    def test_codex_canary_routes_focused_work_to_native_spark_worker(self) -> None:
        intent = self.intent("codex", "implementation")
        intent.update(
            complexity="focused", risk="low", scope="single_module",
            duration="short", durability="session",
            surface_capabilities={"native_subagents": True}, session_id="task-123",
        )

        decision = decide(intent, "codex_canary")

        self.assertEqual("codex_canary", decision["mode"])
        self.assertTrue(decision["enforced"])
        self.assertEqual("native_subagent", decision["lane"])
        self.assertEqual("codex_fast", decision["model_alias"])
        self.assertEqual("spark-worker", decision["worker_profile"])

    def test_v2_native_worker_uses_concrete_spark_model(self) -> None:
        intent = self.intent("codex", "implementation")
        intent.update(
            protocol_version=2, complexity="focused", risk="low",
            scope="single_module", duration="short", durability="session",
            surface_capabilities={
                "durable_agent_jobs": True, "native_subagents": True,
            },
            session_id="task-v2",
        )
        decision = decide(intent, "surface_canary")
        self.assertEqual("native_subagent", decision["lane"])
        self.assertEqual("gpt-5.3-codex-spark", decision["model_alias"])

    def test_codex_canary_requires_native_capability_and_session_identity(self) -> None:
        without_native = self.intent("codex", "implementation")
        without_native.update(complexity="focused", durability="session", session_id="task-123")
        without_session = self.intent("codex", "implementation")
        without_session.update(
            complexity="focused", durability="session",
            surface_capabilities={"native_subagents": True},
        )

        self.assertEqual("direct", decide(without_native, "codex_canary")["lane"])
        self.assertEqual("direct", decide(without_session, "codex_canary")["lane"])

    def test_non_codex_surface_remains_shadow_in_canary_mode(self) -> None:
        decision = decide(self.intent("claude", "planning"), "codex_canary")

        self.assertEqual("shadow", decision["mode"])
        self.assertFalse(decision["enforced"])
