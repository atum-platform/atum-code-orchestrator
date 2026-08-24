from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[2] / "skills/agent-jobs/scripts/delegate.py"


class DelegateClientTest(unittest.TestCase):
    def test_checks_fail_closed_for_unmediated_provider(self) -> None:
        spec = importlib.util.spec_from_file_location("agent_job_delegate_check_test", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as root, patch.object(
            sys, "argv", [
                "delegate.py", "--provider", "kimi", "--model", "kimi-code/k3",
                "--mode", "implement", "--workdir", root, "--prompt", "change",
                "--check", "unit=npm test",
            ],
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                module.main()

        self.assertEqual(2, raised.exception.code)
        self.assertIn("supported only with --provider=claude", stderr.getvalue())

    def test_kimi_semantic_job_prints_events_and_terminal_remainder(self) -> None:
        spec = importlib.util.spec_from_file_location("agent_job_delegate_test", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        terminal = {
            "job": {
                "status": "completed", "semantic_stream": 1,
                "semantic_normalization_failed": 0,
            },
            "cursor": 0,
            "event_cursor": 1,
            "events": [{"kind": "message_delta", "payload": {"text": "part"}}],
            "partial_response": "partial answer",
        }
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ, {"AGENT_JOB_DEPTH": "0"}
        ), patch.object(
            module, "submit", return_value={"job_id": "kimi-job"}
        ), patch.object(module, "read", return_value=terminal), patch.object(
            sys, "argv", [
                "delegate.py", "--provider", "kimi", "--model", "kimi-code/k3",
                "--mode", "readonly", "--workdir", root, "--prompt", "review",
            ]
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                return_code = module.main()

        self.assertEqual(0, return_code)
        self.assertEqual("partial answer", stdout.getvalue())
        self.assertIn("AGENT_JOB_ID=kimi-job", stderr.getvalue())

    def test_suspected_response_loss_is_visible_on_stderr(self) -> None:
        spec = importlib.util.spec_from_file_location("agent_job_delegate_warning_test", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        terminal = {
            "job": {
                "status": "completed", "semantic_stream": 1,
                "semantic_normalization_failed": 0,
            },
            "cursor": 0,
            "event_cursor": 1,
            "events": [{
                "kind": "warning",
                "payload": {"subtype": "suspected_response_loss"},
            }],
            "partial_response": "short",
        }
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ, {"AGENT_JOB_DEPTH": "0"}
        ), patch.object(
            module, "submit", return_value={"job_id": "claude-job"}
        ), patch.object(module, "read", return_value=terminal), patch.object(
            sys, "argv", [
                "delegate.py", "--provider", "claude", "--model", "opus",
                "--mode", "readonly", "--workdir", root, "--prompt", "review",
            ]
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                return_code = module.main()

        self.assertEqual(0, return_code)
        self.assertEqual("short", stdout.getvalue())
        self.assertIn("marked partial", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
