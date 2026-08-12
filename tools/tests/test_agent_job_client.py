from __future__ import annotations

from pathlib import Path
import sys
import unittest


TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

from agent_job_client import _parser  # noqa: E402


class AgentJobClientParserTest(unittest.TestCase):
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
        self.assertEqual({"native_subagents": True}, args["surface_capabilities"])

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


if __name__ == "__main__":
    unittest.main()
