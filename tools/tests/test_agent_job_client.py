from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch


TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

from agent_job_client import _parser, route_decide  # noqa: E402


class AgentJobClientParserTest(unittest.TestCase):
    def test_submit_has_separate_queue_and_run_timeout_defaults(self) -> None:
        args = vars(_parser().parse_args([
            "submit", "--provider", "kimi", "--mode", "readonly",
            "--workdir", "/tmp", "--prompt", "review",
        ]))
        self.assertEqual(900, args["queue_timeout_seconds"])
        self.assertEqual(2700, args["run_timeout_seconds"])
        self.assertIsNone(args["timeout_seconds"])

    def test_event_cursor_is_omitted_for_legacy_reads(self) -> None:
        args = vars(_parser().parse_args(["read", "job-id"]))
        self.assertNotIn("event_cursor", args)

    def test_event_cursor_and_wait_are_available_for_semantic_reads(self) -> None:
        args = vars(_parser().parse_args([
            "read", "job-id", "--event-cursor", "17", "--wait-seconds", "30",
        ]))
        self.assertEqual(17, args["event_cursor"])
        self.assertEqual(30, args["wait_seconds"])

    def test_route_decide_parses_structured_surface_capabilities(self) -> None:
        args = vars(_parser().parse_args([
            "route-decide", "--caller-provider", "codex", "--surface", "codex",
            "--capability", "planning", "--surface-capabilities",
            '{"native_subagents":true}',
        ]))
        self.assertEqual("route-decide", args["action"])
        self.assertEqual(2, args["protocol_version"])
        self.assertEqual({"native_subagents": True}, args["surface_capabilities"])

    def test_route_decide_parses_one_hop_escalation(self) -> None:
        args = vars(_parser().parse_args([
            "route-decide", "--caller-provider", "codex", "--surface", "codex",
            "--capability", "planning", "--session-id", "task",
            "--previous-decision-id", "parent",
            "--escalation-reason", "provider_failure",
            "--escalation-evidence", "provider exited",
        ]))
        self.assertEqual("parent", args["previous_decision_id"])
        self.assertEqual("provider_failure", args["escalation_reason"])
        self.assertEqual("provider exited", args["escalation_evidence"])

    def test_route_lifecycle_commands_parse(self) -> None:
        feedback = vars(_parser().parse_args([
            "route-feedback", "decision", "--session-id", "task",
            "--outcome", "completed",
        ]))
        reconcile = vars(_parser().parse_args([
            "route-reconcile", "--session-id", "task",
            "--active-decision-id", "decision",
        ]))

        self.assertEqual("completed", feedback["outcome"])
        self.assertEqual(["decision"], reconcile["active_decision_id"])

    def test_route_decide_retries_v1_only_for_old_protocol_rejection(self) -> None:
        with patch("agent_job_client.request", side_effect=[
            RuntimeError("Unsupported routing protocol version: 2; expected 1"),
            {"protocol_version": 1},
        ]) as mocked:
            result = route_decide(protocol_version=2, caller_provider="codex")
        self.assertEqual(1, result["protocol_version"])
        self.assertEqual(2, mocked.call_count)
        self.assertEqual(1, mocked.call_args_list[1].args[0]["protocol_version"])


if __name__ == "__main__":
    unittest.main()
