from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

import agent_jobs_server  # noqa: E402
import review_cli  # noqa: E402
import review_core  # noqa: E402


class ReviewCoreTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.workdir = self.root / "project"
        self.workdir.mkdir()
        self.old_roots = os.environ.get("AGENT_JOB_ALLOWED_ROOTS")
        os.environ["AGENT_JOB_ALLOWED_ROOTS"] = str(self.root)

    async def asyncTearDown(self) -> None:
        if self.old_roots is None:
            os.environ.pop("AGENT_JOB_ALLOWED_ROOTS", None)
        else:
            os.environ["AGENT_JOB_ALLOWED_ROOTS"] = self.old_roots
        self.temp.cleanup()

    async def test_missing_roots_fail_closed(self) -> None:
        os.environ["AGENT_JOB_ALLOWED_ROOTS"] = str(self.root / "missing")
        with self.assertRaisesRegex(ValueError, "fail-open"):
            review_core.safe_workdir(str(self.workdir))

    async def test_context_file_must_stay_inside_workdir(self) -> None:
        sibling = self.root / "sibling.txt"
        sibling.write_text("safe", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "inside the selected workdir"):
            review_core.context_files_text([str(sibling)], self.workdir)

    async def test_secret_filename_is_rejected(self) -> None:
        secret = self.workdir / ".env"
        secret.write_text("TOKEN=hidden", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "secret-like"):
            review_core.context_files_text([str(secret)], self.workdir)

    async def test_secret_directory_is_rejected(self) -> None:
        secret = self.workdir / "credentials" / "notes.txt"
        secret.parent.mkdir()
        secret.write_text("hidden", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "private-data store"):
            review_core.context_files_text([str(secret)], self.workdir)

    async def test_prompt_redacts_all_typed_text_inputs(self) -> None:
        prompt = review_core.build_review_prompt(
            "claude", "TOKEN=instruction-secret", workdir=self.workdir,
            context_text="key=sk-abcdefghijklmnopqrstuvwxyz",
            expected_output="password=output-secret",
        )
        self.assertNotIn("instruction-secret", prompt)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", prompt)
        self.assertNotIn("output-secret", prompt)
        self.assertGreaterEqual(prompt.count("[REDACTED]"), 3)

    async def test_submit_is_structurally_readonly_and_forwards_idempotency(self) -> None:
        with patch.object(review_core, "submit", return_value={"job_id": "same"}) as mocked:
            result = review_core.job_submit(
                provider="kimi", model="kimi-code/k3", instructions="review",
                workdir=str(self.workdir), idempotency_key="checkpoint-kimi",
            )
        self.assertEqual("same", result["job_id"])
        kwargs = mocked.call_args.kwargs
        self.assertEqual("readonly", kwargs["mode"])
        self.assertEqual("checkpoint-kimi", kwargs["idempotency_key"])
        self.assertEqual(900, kwargs["queue_timeout_seconds"])
        self.assertEqual(2700, kwargs["run_timeout_seconds"])
        self.assertNotIn("implement_capability", kwargs)

    async def test_mcp_surface_has_only_guarded_generic_tools(self) -> None:
        tools = set(agent_jobs_server.mcp._tool_manager._tools)
        self.assertEqual(
            {
                "route_decide", "route_feedback", "route_reconcile", "route_status",
                "job_submit", "job_read", "job_list", "job_cancel", "job_inbox",
            }, tools
        )
        parameters = inspect.signature(agent_jobs_server.job_submit).parameters
        self.assertNotIn("mode", parameters)
        self.assertNotIn("prompt", parameters)

    async def test_cli_and_mcp_submit_payloads_match(self) -> None:
        calls: list[dict[str, object]] = []

        def fake_submit(**kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {"job_id": "job"}

        values: dict[str, object] = {
            "action": "submit", "provider": "claude", "model": "opus",
            "instructions": "review", "workdir": str(self.workdir),
            "context_git_diff": True, "context_git_base": "HEAD",
            "context_files": None, "context_text": "", "expected_output": "findings",
            "queue_timeout_seconds": 300, "run_timeout_seconds": 600,
            "timeout_seconds": None, "max_turns": 7, "idempotency_key": "same",
            "label": "checkpoint", "owner": "test",
        }
        with patch.object(review_core, "job_submit", side_effect=fake_submit):
            review_cli.dispatch(values.copy())
            await agent_jobs_server.job_submit(**{key: value for key, value in values.items() if key != "action"})
        self.assertEqual(calls[0], calls[1])

    async def test_legacy_max_turns_is_accepted_but_not_forwarded(self) -> None:
        with patch.object(review_core, "submit", return_value={"job_id": "job"}) as mocked:
            review_core.job_submit(
                provider="claude", model="opus", instructions="review",
                workdir=str(self.workdir), max_turns=4,
            )
        self.assertNotIn("max_turns", mocked.call_args.kwargs)

    async def test_cli_dispatch_threads_all_lifecycle_arguments(self) -> None:
        cases = [
            (
                "read",
                {
                    "job_id": "job", "cursor": 7, "event_cursor": 9,
                    "max_bytes": 10, "wait_seconds": 3,
                },
                "job_read",
            ),
            ("list", {"status": "running", "limit": 4, "owner": "codex"}, "job_list"),
            ("cancel", {"job_id": "job"}, "job_cancel"),
            (
                "inbox",
                {"owner": "codex", "limit": 20, "ack_delivery_ids": ["delivery"]},
                "job_inbox",
            ),
        ]
        for action, values, target in cases:
            with self.subTest(action=action), patch.object(
                review_core, target, return_value={"ok": True}
            ) as mocked:
                review_cli.dispatch({"action": action, **values})
                mocked.assert_called_once_with(**values)

    async def test_cli_and_mcp_route_payloads_match(self) -> None:
        calls: list[dict[str, object]] = []

        def fake_route(**kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {"decision_id": "decision"}

        values: dict[str, object] = {
            "protocol_version": 2, "caller_provider": "codex", "surface": "codex",
            "capability": "planning", "complexity": "deep", "risk": "medium",
            "scope": "repo", "duration": "long", "durability": "durable",
            "parallelizable": False, "surface_capabilities": {},
            "explicit_provider": "", "explicit_model": "", "session_id": "task-1",
            "previous_decision_id": "parent", "escalation_reason": "provider_failure",
            "escalation_evidence": "provider exited",
            "owner": "test",
        }
        with patch.object(review_core, "routing_decide", side_effect=fake_route):
            review_cli.dispatch({"action": "route-decide", **values})
            await agent_jobs_server.route_decide(**values)
        self.assertEqual(calls[0], calls[1])

    async def test_cli_dispatch_threads_routing_lifecycle_arguments(self) -> None:
        cases = [
            (
                "route-feedback",
                {"decision_id": "decision", "session_id": "task", "outcome": "completed"},
                "routing_feedback",
            ),
            (
                "route-reconcile",
                {"session_id": "task", "active_decision_id": ["decision"]},
                "routing_reconcile",
            ),
        ]
        for action, values, target in cases:
            with self.subTest(action=action), patch.object(
                review_core, target, return_value={"ok": True}
            ) as mocked:
                review_cli.dispatch({"action": action, **values})
                expected = dict(values)
                if action == "route-reconcile":
                    expected["active_decision_ids"] = expected.pop("active_decision_id")
                mocked.assert_called_once_with(**expected)

    async def test_git_base_includes_committed_branch_changes(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.workdir)], check=True)
        file = self.workdir / "app.py"
        file.write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workdir), "add", "app.py"], check=True)
        env = {**os.environ, "ALLOW_NO_DOCS_LOG": "1"}
        subprocess.run(
            ["git", "-C", str(self.workdir), "-c", "user.name=Test",
             "-c", "user.email=test@example.com", "commit", "-qm", "base"],
            check=True, env=env,
        )
        base = subprocess.check_output(["git", "-C", str(self.workdir), "rev-parse", "HEAD"], text=True).strip()
        file.write_text("after\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workdir), "add", "app.py"], check=True)
        subprocess.run(
            ["git", "-C", str(self.workdir), "-c", "user.name=Test",
             "-c", "user.email=test@example.com", "commit", "-qm", "change"],
            check=True, env=env,
        )
        context = review_core.git_context(self.workdir, base)
        self.assertIn("+after", context)
        self.assertNotIn("+before", context)

    async def test_git_context_includes_untracked_text_files(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.workdir)], check=True)
        tracked = self.workdir / "tracked.txt"
        tracked.write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workdir), "add", "tracked.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(self.workdir), "-c", "user.name=Test",
             "-c", "user.email=test@example.com", "commit", "-qm", "base"],
            check=True, env={**os.environ, "ALLOW_NO_DOCS_LOG": "1"},
        )
        new_file = self.workdir / "new_module.py"
        new_file.write_text("UNTRACKED_REVIEW_MARKER = True\n", encoding="utf-8")
        context = review_core.git_context(self.workdir, "HEAD")
        self.assertIn("untracked file: new_module.py", context)
        self.assertIn("UNTRACKED_REVIEW_MARKER", context)

    async def test_git_context_does_not_follow_untracked_symlinks(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.workdir)], check=True)
        tracked = self.workdir / "tracked.txt"
        tracked.write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workdir), "add", "tracked.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(self.workdir), "-c", "user.name=Test",
             "-c", "user.email=test@example.com", "commit", "-qm", "base"],
            check=True, env={**os.environ, "ALLOW_NO_DOCS_LOG": "1"},
        )
        outside = self.root / "outside.txt"
        outside.write_text("MUST_NOT_LEAK\n", encoding="utf-8")
        (self.workdir / "linked.txt").symlink_to(outside)
        context = review_core.git_context(self.workdir, "HEAD")
        self.assertNotIn("MUST_NOT_LEAK", context)
        self.assertIn("outside the selected workdir", context)


if __name__ == "__main__":
    unittest.main()
