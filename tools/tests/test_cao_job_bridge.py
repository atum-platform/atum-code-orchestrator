from __future__ import annotations

import json
import os
from io import StringIO
from pathlib import Path
import signal
import sys
import unittest
from unittest.mock import patch


TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

from cao_job_bridge import CaoClient, MAX_REQUEST_TIMEOUT, main  # noqa: E402


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.payload


class CaoClientTest(unittest.TestCase):
    def test_request_encodes_body_query_and_bearer_token(self) -> None:
        seen = {}

        def fake_urlopen(request, timeout):
            seen["request"] = request
            seen["timeout"] = timeout
            return _Response({"id": "term-1"})

        client = CaoClient("http://127.0.0.1:9889/", "secret-token")
        with patch("cao_job_bridge.urlopen", side_effect=fake_urlopen):
            result = client.request(
                "POST", "/sessions", params={"provider": "codex"},
                body={"initial_message": "review"}, timeout=3.0,
            )

        self.assertEqual({"id": "term-1"}, result)
        request = seen["request"]
        self.assertEqual("POST", request.method)
        self.assertIn("provider=codex", request.full_url)
        self.assertEqual("Bearer secret-token", request.headers["Authorization"])
        self.assertEqual({"initial_message": "review"}, json.loads(request.data))
        self.assertEqual(3.0, seen["timeout"])


class _FakeClient:
    def __init__(self, workdir: str, statuses: list[str], final: str = "result") -> None:
        self.workdir = workdir
        self.statuses = statuses
        self.final = final
        self.calls = []
        self.poll_failures = 0

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if method == "POST":
            return {
                "id": "abcd1234",
                "session_name": "cao-agent-job-test-job",
                "allowed_tools": ["fs_read", "fs_list"],
            }
        if path.endswith("/working-directory"):
            return {"working_directory": self.workdir}
        if method == "DELETE":
            return {}
        if path.endswith("/output"):
            return {"output": self.final}
        return {"status": self.statuses.pop(0)}


class CaoBridgeLifecycleTest(unittest.TestCase):
    def run_main(self, client: _FakeClient) -> int:
        argv = [
            "cao_job_bridge.py", "--provider", "claude", "--model", "opus",
            "--mode", "readonly", "--workdir", client.workdir, "--job-id", "test-job",
        ]
        with (
            patch("cao_job_bridge.CaoClient", return_value=client),
            patch.object(sys, "argv", argv),
            patch.object(sys, "stdin", StringIO("review this")),
            patch("cao_job_bridge.time.sleep"),
        ):
            return main()

    def test_success_returns_result_and_deletes_session(self) -> None:
        client = _FakeClient(str(TOOLS_DIR), ["processing", "completed"], "final result")

        self.assertEqual(0, self.run_main(client))

        self.assertIn(("DELETE", "/sessions/cao-agent-job-test-job", {"timeout": 3.0}), client.calls)
        post = next(call for call in client.calls if call[0] == "POST")
        body = post[2]["body"]
        self.assertEqual("1", body["env_vars"]["AGENT_JOB_DEPTH"])
        self.assertEqual("claude", body["env_vars"]["AGENT_JOB_PROVIDER"])
        self.assertEqual("test-job", body["env_vars"]["AGENT_JOB_ID"])
        self.assertEqual("agent_job_compat", body["metadata"]["kind"])
        self.assertGreater(body["metadata"]["expires_at"], 0)

    def test_empty_completed_result_fails_and_deletes_session(self) -> None:
        client = _FakeClient(str(TOOLS_DIR), ["completed"], "  ")

        self.assertEqual(1, self.run_main(client))
        self.assertTrue(any(method == "DELETE" for method, _path, _kwargs in client.calls))

    def test_waiting_for_user_is_recoverable(self) -> None:
        client = _FakeClient(
            str(TOOLS_DIR), ["waiting_user_answer", "processing", "completed"], "final result"
        )

        self.assertEqual(0, self.run_main(client))
        self.assertTrue(any(method == "DELETE" for method, _path, _kwargs in client.calls))

    def test_malformed_deadline_falls_back_to_bounded_lease(self) -> None:
        client = _FakeClient(str(TOOLS_DIR), ["completed"], "final result")
        with patch.dict(os.environ, {"AGENT_JOB_DEADLINE_EPOCH": "not-a-number"}):
            self.assertEqual(0, self.run_main(client))
        post = next(call for call in client.calls if call[0] == "POST")
        self.assertGreater(post[2]["body"]["metadata"]["expires_at"], 0)

    def test_workspace_mismatch_fails_closed_and_deletes_session(self) -> None:
        client = _FakeClient(str(TOOLS_DIR.parent), ["completed"])
        argv_workdir = str(TOOLS_DIR)
        argv = [
            "cao_job_bridge.py", "--provider", "claude", "--mode", "readonly",
            "--workdir", argv_workdir, "--job-id", "test-job",
        ]
        with (
            patch("cao_job_bridge.CaoClient", return_value=client),
            patch.object(sys, "argv", argv),
            patch.object(sys, "stdin", StringIO("review this")),
        ):
            self.assertEqual(1, main())
        self.assertTrue(any(method == "DELETE" for method, _path, _kwargs in client.calls))

    def test_readonly_tool_policy_mismatch_fails_closed_and_deletes_session(self) -> None:
        client = _FakeClient(str(TOOLS_DIR), ["completed"])
        original = client.request

        def mismatched(method, path, **kwargs):
            result = original(method, path, **kwargs)
            if method == "POST":
                result["allowed_tools"] = ["*"]
            return result

        client.request = mismatched

        self.assertEqual(1, self.run_main(client))
        self.assertTrue(any(method == "DELETE" for method, _path, _kwargs in client.calls))

    def test_transient_poll_failure_retries(self) -> None:
        client = _FakeClient(str(TOOLS_DIR), ["completed"], "final result")
        original = client.request

        def flaky(method, path, **kwargs):
            if method == "GET" and path == "/terminals/abcd1234" and client.poll_failures == 0:
                client.poll_failures += 1
                raise RuntimeError("CAO unavailable at http://127.0.0.1:9889")
            return original(method, path, **kwargs)

        client.request = flaky

        self.assertEqual(0, self.run_main(client))
        self.assertEqual(1, client.poll_failures)

    def test_signal_requests_cleanup(self) -> None:
        client = _FakeClient(str(TOOLS_DIR), ["processing"])
        handlers = {}
        original = client.request

        def stopping(method, path, **kwargs):
            if method == "GET" and path == "/terminals/abcd1234":
                handlers[signal.SIGTERM](signal.SIGTERM, None)
            return original(method, path, **kwargs)

        client.request = stopping
        argv = [
            "cao_job_bridge.py", "--provider", "claude", "--mode", "readonly",
            "--workdir", str(TOOLS_DIR), "--job-id", "test-job",
        ]

        def remember(sig, handler):
            handlers[sig] = handler

        with (
            patch("cao_job_bridge.CaoClient", return_value=client),
            patch("cao_job_bridge.signal.signal", side_effect=remember),
            patch.object(sys, "argv", argv),
            patch.object(sys, "stdin", StringIO("review this")),
            patch("cao_job_bridge.time.sleep"),
        ):
            self.assertEqual(130, main())
        self.assertTrue(any(method == "DELETE" for method, _path, _kwargs in client.calls))

    def test_signal_interrupts_blocking_launch_and_uses_bounded_timeout(self) -> None:
        client = _FakeClient(str(TOOLS_DIR), ["completed"])
        handlers = {}
        seen_timeout = []

        def blocking(method, path, **kwargs):
            client.calls.append((method, path, kwargs))
            if method == "POST":
                seen_timeout.append(kwargs["timeout"])
                handlers[signal.SIGTERM](signal.SIGTERM, None)
            return {}

        client.request = blocking
        argv = [
            "cao_job_bridge.py", "--provider", "claude", "--mode", "readonly",
            "--workdir", str(TOOLS_DIR), "--job-id", "test-job",
        ]

        def remember(sig, handler):
            handlers[sig] = handler

        with (
            patch("cao_job_bridge.CaoClient", return_value=client),
            patch("cao_job_bridge.signal.signal", side_effect=remember),
            patch.object(sys, "argv", argv),
            patch.object(sys, "stdin", StringIO("review this")),
        ):
            self.assertEqual(130, main())
        self.assertEqual([MAX_REQUEST_TIMEOUT], seen_timeout)
        self.assertTrue(any(method == "DELETE" for method, _path, _kwargs in client.calls))


if __name__ == "__main__":
    unittest.main()
