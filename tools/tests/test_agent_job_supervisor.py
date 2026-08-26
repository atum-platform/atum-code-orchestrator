from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

from agent_job_client import request  # noqa: E402
import agent_job_supervisor as supervisor_module  # noqa: E402
from agent_job_supervisor import JobStore, Supervisor  # noqa: E402


class JobStoreMigrationTest(unittest.TestCase):
    def test_existing_jobs_gain_nullable_separate_timeout_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = JobStore(Path(temporary) / "jobs.sqlite3")
            columns = {
                str(row["name"]): row
                for row in store.db.execute("PRAGMA table_info(jobs)")
            }
            self.assertIn("queue_timeout_seconds", columns)
            self.assertIn("run_timeout_seconds", columns)
            self.assertEqual(0, columns["queue_timeout_seconds"]["notnull"])
            self.assertEqual(0, columns["run_timeout_seconds"]["notnull"])
            store.db.close()

    def test_existing_shadow_route_table_gains_canary_lifecycle_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "jobs.sqlite3"
            db = sqlite3.connect(path)
            db.execute(
                """CREATE TABLE route_decisions (
                    decision_id TEXT PRIMARY KEY, protocol_version INTEGER NOT NULL,
                    policy_version TEXT NOT NULL, mode TEXT NOT NULL,
                    caller_provider TEXT NOT NULL, surface TEXT NOT NULL,
                    capability TEXT NOT NULL, lane TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT '', model_alias TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL, expires_at REAL,
                    owner TEXT NOT NULL DEFAULT '', request_json TEXT NOT NULL,
                    response_json TEXT NOT NULL
                )"""
            )
            db.commit()
            db.close()

            store = JobStore(path)
            columns = {
                str(row["name"])
                for row in store.db.execute("PRAGMA table_info(route_decisions)")
            }

            self.assertTrue({
                "session_id", "reservation_status", "feedback_outcome", "feedback_at",
                "parent_decision_id", "escalation_hop", "escalation_reason",
                "escalation_evidence",
            }.issubset(columns))

    def test_rate_limit_cooldown_never_shortens_and_events_prune(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = JobStore(Path(temporary) / "jobs.sqlite3")
            store.record_provider_rate_limit("kimi", 2_000, "first")
            store.record_provider_rate_limit("kimi", 1_500, "second")
            row = store.db.execute(
                "SELECT cooldown_until FROM provider_health WHERE provider = 'kimi'"
            ).fetchone()
            self.assertEqual(2_000, row["cooldown_until"])
            store.db.execute("UPDATE provider_health_events SET observed_at = 1")
            store.db.commit()
            store.prune(2)
            count = store.db.execute(
                "SELECT COUNT(*) FROM provider_health_events"
            ).fetchone()[0]
            self.assertEqual(0, count)
            store.db.close()

    def test_invalid_routing_mode_fails_at_supervisor_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"AGENT_JOB_ROUTING_MODE": "invalid"}
        ):
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "AGENT_JOB_ROUTING_MODE"):
                Supervisor(
                    state_dir=root / "state", socket_path=root / "state/socket",
                    db_path=root / "state/jobs.sqlite3", log_dir=root / "state/logs",
                )

    def test_invalid_native_routing_numbers_name_the_environment_variable(self) -> None:
        for name in (
            "AGENT_JOB_CODEX_NATIVE_RESERVATIONS",
            "AGENT_JOB_ROUTE_RESERVATION_SECONDS",
            "AGENT_JOB_QUOTA_STALE_SECONDS",
            "AGENT_JOB_RATE_LIMIT_COOLDOWN_SECONDS",
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary, patch.dict(
                os.environ, {name: "invalid"}
            ):
                root = Path(temporary)
                with self.assertRaisesRegex(ValueError, name):
                    Supervisor(
                        state_dir=root / "state", socket_path=root / "state/socket",
                        db_path=root / "state/jobs.sqlite3", log_dir=root / "state/logs",
                    )

    def test_invalid_quota_routing_flag_fails_at_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"AGENT_JOB_QUOTA_ROUTING": "sometimes"}
        ):
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "AGENT_JOB_QUOTA_ROUTING"):
                Supervisor(
                    state_dir=root / "state", socket_path=root / "state/socket",
                    db_path=root / "state/jobs.sqlite3", log_dir=root / "state/logs",
                )

    def test_dynamic_concurrency_requires_quota_routing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {"AGENT_JOB_DYNAMIC_CONCURRENCY": "1", "AGENT_JOB_QUOTA_ROUTING": "0"},
        ):
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "requires AGENT_JOB_QUOTA_ROUTING"):
                Supervisor(
                    state_dir=root / "state", socket_path=root / "state/socket",
                    db_path=root / "state/jobs.sqlite3", log_dir=root / "state/logs",
                )

    def test_provider_concurrency_is_bounded_to_one_through_three(self) -> None:
        for raw, expected in (("0", 1), ("1", 1), ("3", 3), ("5", 3)):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as temporary, patch.dict(
                os.environ, {"AGENT_JOB_CLAUDE_CONCURRENCY": raw}
            ):
                root = Path(temporary)
                supervisor = Supervisor(
                    state_dir=root / "state", socket_path=root / "state/socket",
                    db_path=root / "state/jobs.sqlite3", log_dir=root / "state/logs",
                )
                self.assertEqual(expected, supervisor.provider_limits["claude"])
                supervisor.store.db.close()
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"AGENT_JOB_CLAUDE_CONCURRENCY": "invalid"}
        ):
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "AGENT_JOB_CLAUDE_CONCURRENCY"):
                Supervisor(
                    state_dir=root / "state", socket_path=root / "state/socket",
                    db_path=root / "state/jobs.sqlite3", log_dir=root / "state/logs",
                )


def fake_command(job: dict[str, object]) -> tuple[list[str], str | None, dict[str, str]]:
    prompt = str(job["prompt"])
    if prompt == "complete":
        script = "import time; print('first', flush=True); time.sleep(.1); print('second', flush=True)"
    elif prompt == "delayed":
        script = "import time; time.sleep(1); print('delayed output', flush=True); time.sleep(.4)"
    elif prompt == "rapid-output":
        script = "import time; print('first', flush=True); time.sleep(.2); print('rapid second', flush=True); time.sleep(2)"
    elif prompt == "slow":
        script = "import time; print('started', flush=True); time.sleep(30)"
    elif prompt == "codex-events":
        script = """import json, time
events = [
    {"type": "thread.started", "thread_id": "thread-1"},
    {"type": "turn.started"},
    {"type": "item.started", "item": {"id": "tool-1", "type": "command_execution"}},
    {"type": "item.completed", "item": {"id": "tool-1", "type": "command_execution", "exit_code": 0}},
    {"type": "item.completed", "item": {"id": "message-1", "type": "agent_message", "text": "partial answer"}},
    {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 2}},
]
for event in events:
    print(json.dumps(event), flush=True)
    time.sleep(.08)
"""
    elif prompt == "codex-partial-slow":
        script = """import json, time
print(json.dumps({"type": "turn.started"}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"id": "message-1", "type": "agent_message", "text": "recover me"}}), flush=True)
time.sleep(30)
"""
    elif prompt == "codex-tool-slow":
        script = """import json, time
print(json.dumps({"type": "turn.started"}), flush=True)
time.sleep(.5)
print(json.dumps({"type": "item.started", "item": {"id": "tool-1", "type": "command_execution"}}), flush=True)
time.sleep(30)
"""
    elif prompt == "codex-raw-slow":
        script = """import json, time
print(json.dumps({"type": "future.event", "value": 1}), flush=True)
time.sleep(30)
"""
    elif prompt == "claude-events":
        script = """import json, time
events = [
    {"type": "system", "subtype": "init", "model": "claude-opus-5", "claude_code_version": "test"},
    {"type": "system", "subtype": "status", "status": "requesting"},
    {"type": "stream_event", "event": {"type": "message_start", "message": {"model": "claude-opus-5"}}},
    {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "partial "}}},
    {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "answer"}}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "partial answer"}]}},
    {"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "partial answer", "usage": {"output_tokens": 2}},
]
for event in events:
    print(json.dumps(event), flush=True)
    time.sleep(.05)
"""
    elif prompt == "claude-partial-slow":
        script = """import json, time
print(json.dumps({"type": "stream_event", "event": {"type": "message_start", "message": {"model": "claude-opus-5"}}}), flush=True)
print(json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "recover opus"}}}), flush=True)
time.sleep(30)
"""
    elif prompt == "claude-tool-slow":
        script = """import json, time
print(json.dumps({"type": "stream_event", "event": {"type": "message_start", "message": {"model": "claude-opus-5"}}}), flush=True)
print(json.dumps({"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "tool-1", "name": "Read", "input": {}}}}), flush=True)
print(json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "secret-input"}}}), flush=True)
time.sleep(30)
"""
    elif prompt == "claude-waiting-slow":
        script = """import json, time
print(json.dumps({"type": "system", "subtype": "status", "status": "requesting"}), flush=True)
time.sleep(30)
"""
    elif prompt == "claude-error-zero":
        script = """import json
print(json.dumps({"type": "stream_event", "event": {"type": "message_start", "message": {"id": "m"}}}), flush=True)
print(json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "unfinished"}}}), flush=True)
print(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True}), flush=True)
"""
    elif prompt == "claude-snapshot-collision":
        script = """import json
events = [
    {"type": "stream_event", "event": {"type": "message_start", "message": {"id": "stream-id"}}},
    {"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}}},
    {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": ""}}},
    {"type": "assistant", "message": {"id": "snapshot-id", "content": [{"type": "thinking", "thinking": ""}]}},
    {"type": "assistant", "message": {"id": "snapshot-id", "content": [{"type": "text", "text": "snapshot answer"}]}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "snapshot answer"},
]
for event in events:
    print(json.dumps(event), flush=True)
"""
    elif prompt == "claude-result-only":
        script = """import json
print(json.dumps({"type": "assistant", "parent_tool_use_id": "nested", "message": {"content": [{"type": "text", "text": "private nested text"}]}}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "terminal answer", "usage": {"output_tokens": 2}}), flush=True)
"""
    elif prompt == "claude-result-only-error":
        script = """import json
print(json.dumps({"type": "assistant", "parent_tool_use_id": "nested", "message": {"content": [{"type": "text", "text": "private nested text"}]}}), flush=True)
print(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "private error prose"}), flush=True)
"""
    elif prompt == "claude-mixed-loss":
        script = """import json
print(json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "short"}}}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "x" * 600}), flush=True)
"""
    elif prompt == "kimi-events":
        script = """import json, time
events = [
    {"role": "meta", "type": "system.version", "version": "0.34.0"},
    {"role": "assistant", "content": "partial ", "tool_calls": [{"type": "function", "id": "tc-1", "function": {"name": "ReadFile", "arguments": "secret-input"}}]},
    {"role": "tool", "tool_call_id": "tc-1", "content": "secret-result"},
    {"role": "assistant", "content": "answer"},
]
for event in events:
    print(json.dumps(event), flush=True)
    time.sleep(.05)
"""
    elif prompt == "kimi-partial-slow":
        script = """import json, time
print(json.dumps({"role": "meta", "type": "system.version", "version": "0.34.0"}), flush=True)
print(json.dumps({"role": "assistant", "content": "recover kimi"}), flush=True)
time.sleep(30)
"""
    elif prompt == "kimi-stderr-slow":
        script = """import json, sys, time
print(json.dumps({"role": "meta", "type": "system.version", "version": "0.34.0"}), flush=True)
time.sleep(.2)
print("tool progress", file=sys.stderr, flush=True)
time.sleep(30)
"""
    elif prompt == "kimi-quota-fail":
        script = """import json, sys
print(json.dumps({"role": "meta", "type": "system.version", "version": "0.34.0"}), flush=True)
print("usage limit reached", file=sys.stderr, flush=True)
raise SystemExit(1)
"""
    elif prompt == "kimi-billing-cycle-fail":
        script = """import sys
print("403 You've reached your usage limit for this billing cycle. Your quota will be refreshed in the next cycle.", file=sys.stderr, flush=True)
raise SystemExit(1)
"""
    elif prompt == "quota-subject-fail":
        script = """import sys
print("reviewing quota and 429 handling", flush=True)
print("unrelated syntax failure", file=sys.stderr, flush=True)
raise SystemExit(1)
"""
    else:
        script = "print('unknown', flush=True)"
    return [sys.executable, "-u", "-c", script], None, os.environ.copy()


class SupervisorIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.workdir = root / "work"
        self.workdir.mkdir()
        self.old_roots = os.environ.get("AGENT_JOB_ALLOWED_ROOTS")
        self.old_allow_implement = os.environ.get("AGENT_JOB_ALLOW_IMPLEMENT")
        self.old_kimi_semantic = os.environ.get("AGENT_JOB_KIMI_SEMANTIC")
        self.old_quota_history = os.environ.get("AGENT_JOB_QUOTA_HISTORY_DIR")
        self.old_token_path = supervisor_module.IMPLEMENT_TOKEN_PATH
        os.environ["AGENT_JOB_ALLOWED_ROOTS"] = str(root)
        self.implement_token = root / "implement.token"
        self.implement_token.write_text("test-capability\n", encoding="utf-8")
        os.environ["AGENT_JOB_ALLOW_IMPLEMENT"] = "1"
        os.environ["AGENT_JOB_KIMI_SEMANTIC"] = "0"
        os.environ["AGENT_JOB_QUOTA_HISTORY_DIR"] = str(root / "quota-history")
        supervisor_module.IMPLEMENT_TOKEN_PATH = self.implement_token
        self.launch_counts: dict[str, int] = {}

        def counted_command(job: dict[str, object]) -> tuple[list[str], str | None, dict[str, str]]:
            job_id = str(job["job_id"])
            self.launch_counts[job_id] = self.launch_counts.get(job_id, 0) + 1
            return fake_command(job)

        self.supervisor = Supervisor(
            state_dir=root / "state",
            socket_path=root / "state" / "supervisor.sock",
            db_path=root / "state" / "jobs.sqlite3",
            log_dir=root / "state" / "logs",
            command_builder=counted_command,
            binary_finder=lambda provider: sys.executable,
        )
        self.supervisor.provider_limits = {"claude": 1, "kimi": 1, "codex": 1}
        self.server_task = asyncio.create_task(self.supervisor.serve())
        for _ in range(100):
            if self.supervisor.socket_path.exists():
                break
            await asyncio.sleep(.01)
        self.assertTrue(self.supervisor.socket_path.exists())

    async def asyncTearDown(self) -> None:
        self.server_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.server_task
        if self.old_roots is None:
            os.environ.pop("AGENT_JOB_ALLOWED_ROOTS", None)
        else:
            os.environ["AGENT_JOB_ALLOWED_ROOTS"] = self.old_roots
        if self.old_allow_implement is None:
            os.environ.pop("AGENT_JOB_ALLOW_IMPLEMENT", None)
        else:
            os.environ["AGENT_JOB_ALLOW_IMPLEMENT"] = self.old_allow_implement
        if self.old_kimi_semantic is None:
            os.environ.pop("AGENT_JOB_KIMI_SEMANTIC", None)
        else:
            os.environ["AGENT_JOB_KIMI_SEMANTIC"] = self.old_kimi_semantic
        if self.old_quota_history is None:
            os.environ.pop("AGENT_JOB_QUOTA_HISTORY_DIR", None)
        else:
            os.environ["AGENT_JOB_QUOTA_HISTORY_DIR"] = self.old_quota_history
        supervisor_module.IMPLEMENT_TOKEN_PATH = self.old_token_path
        self.temp.cleanup()

    async def call(self, payload: dict[str, object]) -> dict[str, object]:
        return await asyncio.to_thread(request, payload, self.supervisor.socket_path)

    def spec(self, prompt: str) -> dict[str, object]:
        return {
            "action": "submit",
            "provider": "claude",
            "model": "test-model",
            "mode": "readonly",
            "workdir": str(self.workdir),
            "prompt": prompt,
            "timeout_seconds": 30,
            "soft_stall_seconds": 30,
            "max_turns": 1,
            "caller_depth": 0,
        }

    async def wait_for(self, job_id: str, statuses: set[str], timeout: float = 5) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = await self.call({"action": "read", "job_id": job_id})
            job = result["job"]
            if job["status"] in statuses:
                return result
            await asyncio.sleep(.05)
        self.fail(f"Job {job_id} did not reach {statuses}")

    async def test_completion_and_cursor_reads(self) -> None:
        with patch.dict(os.environ, {"AGENT_JOB_KIMI_SEMANTIC": "0"}):
            spec = self.spec("complete")
            spec["provider"] = "kimi"
            submitted = await self.call(spec)
            self.assertEqual(900, submitted["queue_timeout_seconds"])
            self.assertEqual(30, submitted["run_timeout_seconds"])
            self.assertEqual("separate", submitted["timeout_semantics"])
            result = await self.wait_for(str(submitted["job_id"]), {"completed"})
            self.assertIn("first", result["output"])
            self.assertEqual("first\nsecond\n", result["stdout"])
            self.assertEqual("", result["stderr"])
            cursor = int(result["cursor"])
            again = await self.call({"action": "read", "job_id": submitted["job_id"], "cursor": cursor})
            self.assertEqual("", again["output"])
            self.assertNotIn("prompt", result["job"])

    async def test_legacy_timeout_input_aliases_run_budget(self) -> None:
        spec = self.spec("complete")
        spec.update({
            "queue_timeout_seconds": 300,
            "run_timeout_seconds": 600,
            "timeout_seconds": 45,
        })
        submitted = await self.call(spec)
        self.assertEqual(300, submitted["queue_timeout_seconds"])
        self.assertEqual(45, submitted["run_timeout_seconds"])
        self.assertEqual(45, submitted["timeout_seconds"])
        await self.wait_for(str(submitted["job_id"]), {"completed"})

    async def test_owner_inbox_redelivers_until_exact_owner_acknowledges(self) -> None:
        spec = self.spec("complete")
        spec["owner"] = "codex:phase-6"
        submitted = await self.call(spec)
        await self.wait_for(str(submitted["job_id"]), {"completed"})

        first = await self.call({"action": "inbox", "owner": "codex:phase-6"})
        self.assertEqual(1, len(first["deliveries"]))
        delivery_id = first["deliveries"][0]["delivery_id"]
        self.assertEqual(submitted["job_id"], first["deliveries"][0]["job"]["job_id"])
        again = await self.call({"action": "inbox", "owner": "codex:phase-6"})
        self.assertEqual([delivery_id], [item["delivery_id"] for item in again["deliveries"]])

        await self.call({
            "action": "inbox", "owner": "different-owner",
            "ack_delivery_ids": [delivery_id],
        })
        still_pending = await self.call({"action": "inbox", "owner": "codex:phase-6"})
        self.assertEqual(1, len(still_pending["deliveries"]))
        acknowledged = await self.call({
            "action": "inbox", "owner": "codex:phase-6",
            "ack_delivery_ids": [delivery_id],
        })
        self.assertEqual([], acknowledged["deliveries"])

    async def test_server_side_wait_wakes_on_terminal_transition(self) -> None:
        spec = self.spec("slow")
        spec["provider"] = "kimi"
        submitted = await self.call(spec)
        await self.wait_for(str(submitted["job_id"]), {"running"})
        for _ in range(100):
            current = await self.call({
                "action": "read", "job_id": submitted["job_id"], "max_bytes": 64_000,
            })
            if "started" in current["output"]:
                break
            await asyncio.sleep(.01)
        else:
            self.fail("slow fixture did not emit its initial output")
        waiter = asyncio.create_task(self.call({
            "action": "read", "job_id": submitted["job_id"],
            "cursor": current["cursor"], "max_bytes": 64_000, "wait_seconds": 5,
        }))
        await asyncio.sleep(.1)
        await self.call({"action": "cancel", "job_id": submitted["job_id"]})
        result = await asyncio.wait_for(waiter, timeout=3)
        self.assertEqual("cancelled", result["job"]["status"])

    async def test_server_side_wait_wakes_on_new_output(self) -> None:
        spec = self.spec("delayed")
        spec["provider"] = "kimi"
        submitted = await self.call(spec)
        await self.wait_for(str(submitted["job_id"]), {"running"})
        current = await self.call({"action": "read", "job_id": submitted["job_id"]})
        result = await self.call({
            "action": "read", "job_id": submitted["job_id"],
            "cursor": current["cursor"], "max_bytes": 64_000, "wait_seconds": 3,
        })
        self.assertIn("delayed output", result["output"])
        await self.wait_for(str(submitted["job_id"]), {"completed"})

    async def test_server_side_wait_wakes_on_output_inside_timestamp_throttle(self) -> None:
        spec = self.spec("rapid-output")
        spec["provider"] = "kimi"
        submitted = await self.call(spec)
        await self.wait_for(str(submitted["job_id"]), {"running"})
        for _ in range(100):
            current = await self.call({"action": "read", "job_id": submitted["job_id"]})
            if "first" in current["output"]:
                break
            await asyncio.sleep(.01)
        else:
            self.fail("rapid fixture did not emit its first chunk")
        result = await self.call({
            "action": "read", "job_id": submitted["job_id"],
            "cursor": current["cursor"], "wait_seconds": 3,
        })
        self.assertIn("rapid second", result["output"])
        await self.call({"action": "cancel", "job_id": submitted["job_id"]})
        await self.wait_for(str(submitted["job_id"]), {"cancelled"})

    async def test_concurrent_waiters_share_transition_notification(self) -> None:
        spec = self.spec("slow")
        spec["provider"] = "kimi"
        submitted = await self.call(spec)
        await self.wait_for(str(submitted["job_id"]), {"running"})
        for _ in range(100):
            current = await self.call({"action": "read", "job_id": submitted["job_id"]})
            if "started" in current["output"]:
                break
            await asyncio.sleep(.01)
        else:
            self.fail("slow fixture did not emit its initial output")
        payload = {
            "action": "read", "job_id": submitted["job_id"],
            "cursor": current["cursor"], "wait_seconds": 5,
        }
        waiters = [asyncio.create_task(self.call(payload)) for _ in range(2)]
        await asyncio.sleep(.1)
        await self.call({"action": "cancel", "job_id": submitted["job_id"]})
        results = await asyncio.gather(*waiters)
        self.assertEqual(["cancelled", "cancelled"], [item["job"]["status"] for item in results])

    async def test_zero_max_turns_is_preserved_as_unlimited(self) -> None:
        spec = self.spec("complete")
        spec["max_turns"] = 0
        submitted = await self.call(spec)
        self.assertEqual(0, submitted["max_turns"])
        await self.wait_for(str(submitted["job_id"]), {"completed"})

    async def test_cancel_running_process_group(self) -> None:
        spec = self.spec("slow")
        spec["provider"] = "kimi"
        submitted = await self.call(spec)
        await self.wait_for(str(submitted["job_id"]), {"running"})
        before_cancel = await self.call({
            "action": "read", "job_id": submitted["job_id"], "event_cursor": 0,
        })
        waiter = asyncio.create_task(self.call({
            "action": "read", "job_id": submitted["job_id"],
            "cursor": before_cancel["cursor"],
            "event_cursor": before_cancel["event_cursor"],
            "wait_seconds": 5,
        }))
        await self.call({"action": "cancel", "job_id": submitted["job_id"]})
        terminal = await asyncio.wait_for(waiter, timeout=5)
        event_kinds = [event["kind"] for event in terminal["events"]]
        deadline = time.monotonic() + 10
        while terminal["job"]["lifecycle_status"] not in {"cancelled", "failed", "interrupted"}:
            self.assertLess(time.monotonic(), deadline)
            terminal = await self.call({
                "action": "read", "job_id": submitted["job_id"],
                "cursor": terminal["cursor"],
                "event_cursor": terminal["event_cursor"],
                "wait_seconds": 5,
            })
            event_kinds.extend(event["kind"] for event in terminal["events"])
        self.assertEqual("cancelled", terminal["job"]["lifecycle_status"])
        self.assertIn("job_terminal", event_kinds)
        result = await self.wait_for(str(submitted["job_id"]), {"cancelled"})
        self.assertEqual("cancelled", result["job"]["failure_kind"])

    async def test_quiet_running_job_is_classified_as_possibly_stalled(self) -> None:
        spec = self.spec("slow")
        spec["provider"] = "kimi"
        submitted = await self.call(spec)
        await self.wait_for(str(submitted["job_id"]), {"running"})
        for _ in range(100):
            current = await self.call({"action": "read", "job_id": submitted["job_id"]})
            if "started" in current["output"]:
                break
            await asyncio.sleep(.01)
        else:
            self.fail("quiet fixture did not emit its initial output")
        self.supervisor.store.update(
            str(submitted["job_id"]), last_output_at=time.time() - 31, soft_stall_seconds=30
        )
        result = await self.call({"action": "read", "job_id": submitted["job_id"]})
        self.assertEqual("possibly_stalled", result["job"]["status"])
        self.assertGreaterEqual(result["job"]["seconds_without_output"], 30)
        await self.call({"action": "cancel", "job_id": submitted["job_id"]})
        await self.wait_for(str(submitted["job_id"]), {"cancelled"})

    async def test_codex_events_are_cursor_readable_and_reconstruct_result(self) -> None:
        spec = self.spec("codex-events")
        spec["provider"] = "codex"
        submitted = await self.call(spec)
        result = await self.wait_for(str(submitted["job_id"]), {"completed"})
        legacy = await self.call({
            "action": "read", "job_id": submitted["job_id"],
        })
        self.assertEqual([], legacy["events"])
        self.assertEqual(legacy["event_size"], legacy["event_cursor"])
        result = await self.call({
            "action": "read", "job_id": submitted["job_id"], "event_cursor": 0,
        })

        kinds = [event["kind"] for event in result["events"]]
        self.assertIn("job_started", kinds)
        self.assertIn("tool_started", kinds)
        self.assertIn("tool_finished", kinds)
        self.assertIn("message_delta", kinds)
        self.assertIn("job_terminal", kinds)
        self.assertEqual("partial answer", result["partial_response"])
        self.assertEqual("complete", result["partial_result_state"])
        self.assertTrue(result["job"]["has_partial_response"])
        job = self.supervisor.store.get(str(submitted["job_id"]))
        self.assertEqual(0o600, stat.S_IMODE(Path(f"{job['log_path']}.events.jsonl").stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(Path(f"{job['log_path']}.partial.txt").stat().st_mode))
        again = await self.call({
            "action": "read", "job_id": submitted["job_id"],
            "event_cursor": result["event_cursor"],
        })
        self.assertEqual([], again["events"])

    async def test_oversized_journal_record_never_wedges_event_cursor(self) -> None:
        submitted = await self.call(self.spec("complete"))
        await self.wait_for(str(submitted["job_id"]), {"completed"})
        job = self.supervisor.store.get(str(submitted["job_id"]))
        event_path = Path(f"{job['log_path']}.events.jsonl")
        cursor = event_path.stat().st_size
        with event_path.open("ab") as handle:
            handle.write(json.dumps({"payload": "x" * 100_000}).encode() + b"\n")

        result = await self.call({
            "action": "read", "job_id": submitted["job_id"],
            "event_cursor": cursor,
        })

        self.assertGreater(result["event_cursor"], cursor)
        self.assertEqual(result["event_size"], result["event_cursor"])
        self.assertEqual("truncated_event", result["events"][0]["kind"])

    async def test_normalization_failure_does_not_stop_stdout_capture(self) -> None:
        original = self.supervisor._record_event

        def fail_provider_raw(job_id, kind, payload=None, *, force=False):
            if kind == "provider_raw":
                raise OSError("injected journal failure")
            return original(job_id, kind, payload, force=force)

        with patch.object(self.supervisor, "_record_event", side_effect=fail_provider_raw):
            spec = self.spec("codex-events")
            spec["provider"] = "codex"
            submitted = await self.call(spec)
            result = await self.wait_for(str(submitted["job_id"]), {"completed"})

        self.assertIn('"type": "thread.started"', result["stdout"])
        self.assertIn('"type": "turn.completed"', result["stdout"])
        self.assertIn("Semantic event normalization disabled", result["stderr"])

    async def test_normalization_failure_falls_back_to_output_liveness(self) -> None:
        original = self.supervisor._record_event

        def fail_provider_raw(job_id, kind, payload=None, *, force=False):
            if kind == "provider_raw":
                raise OSError("injected journal failure")
            return original(job_id, kind, payload, force=force)

        with patch.object(self.supervisor, "_record_event", side_effect=fail_provider_raw):
            spec = self.spec("codex-raw-slow")
            spec["provider"] = "codex"
            submitted = await self.call(spec)
            job_id = str(submitted["job_id"])
            for _ in range(100):
                if job_id in self.supervisor.normalization_failed:
                    break
                await asyncio.sleep(.02)
            else:
                self.fail("Injected normalization failure was not observed")
            self.supervisor.event_summaries[job_id]["last_progress_at"] = time.time() - 60
            self.supervisor.store.update(job_id, soft_stall_seconds=30)

            current = await self.call({"action": "read", "job_id": job_id})

            self.assertEqual("running", current["job"]["status"])
            self.assertLess(current["job"]["seconds_without_progress"], 3)
            await self.call({"action": "cancel", "job_id": job_id})
            await self.wait_for(job_id, {"cancelled"})

    async def test_unicode_message_chunks_remain_reconstructable(self) -> None:
        source = "🙂" * 10_000
        chunks = self.supervisor._split_event_payloads("message_delta", {"text": source})
        self.assertEqual(source, "".join(chunk["text"] for chunk in chunks))
        self.assertTrue(all(
            len(chunk["text"].encode("utf-8")) <= supervisor_module.MAX_EVENT_TEXT_CHARS
            for chunk in chunks
        ))

    async def test_journal_budget_exhaustion_is_reported(self) -> None:
        with patch.object(supervisor_module, "MAX_EVENT_LOG_BYTES", 300):
            spec = self.spec("codex-events")
            spec["provider"] = "codex"
            submitted = await self.call(spec)
            result = await self.wait_for(str(submitted["job_id"]), {"completed"})

        self.assertEqual(1, result["job"]["journal_truncated"])

    async def test_terminal_job_without_task_forgets_event_state(self) -> None:
        submitted = self.supervisor.submit(self.spec("complete"))
        job_id = str(submitted["job_id"])

        self.supervisor._finish_job(job_id, "cancelled", "cancelled", "test")

        self.assertNotIn(job_id, self.supervisor.event_summaries)
        self.assertNotIn(job_id, self.supervisor.event_sequences)

    async def test_completed_nonsemantic_provider_reports_partial_unavailable(self) -> None:
        with patch.dict(os.environ, {"AGENT_JOB_KIMI_SEMANTIC": "0"}):
            spec = self.spec("complete")
            spec["provider"] = "kimi"
            submitted = await self.call(spec)
            result = await self.wait_for(str(submitted["job_id"]), {"completed"})
            self.assertEqual("unavailable", result["partial_result_state"])

    async def test_kimi_events_are_private_and_reconstruct_result(self) -> None:
        os.environ["AGENT_JOB_KIMI_SEMANTIC"] = "1"
        spec = self.spec("kimi-events")
        spec["provider"] = "kimi"
        submitted = await self.call(spec)
        result = await self.wait_for(str(submitted["job_id"]), {"completed"})
        result = await self.call({
            "action": "read", "job_id": submitted["job_id"], "event_cursor": 0,
        })

        self.assertEqual("partial answer", result["partial_response"])
        self.assertEqual("complete", result["partial_result_state"])
        self.assertEqual("", result["output"])
        self.assertEqual("", result["stdout"])
        self.assertNotIn("secret", json.dumps(result["events"]))
        kinds = [event["kind"] for event in result["events"]]
        self.assertIn("message_delta", kinds)
        self.assertIn("tool_finished", kinds)
        self.assertNotIn("tool_started", kinds)
        raw = Path(f"{result['job']['log_path']}.stdout")
        self.assertTrue(raw.is_file())
        self.assertEqual(0o600, stat.S_IMODE(raw.stat().st_mode))

    async def test_cancelled_kimi_job_retains_partial_response(self) -> None:
        os.environ["AGENT_JOB_KIMI_SEMANTIC"] = "1"
        spec = self.spec("kimi-partial-slow")
        spec["provider"] = "kimi"
        submitted = await self.call(spec)
        job_id = str(submitted["job_id"])
        for _ in range(100):
            current = await self.call({"action": "read", "job_id": job_id})
            if current["job"]["has_partial_response"]:
                break
            await asyncio.sleep(.02)
        else:
            self.fail("Kimi fixture did not emit a partial response")
        await self.call({"action": "cancel", "job_id": job_id})
        result = await self.wait_for(job_id, {"cancelled"})
        self.assertEqual("recover kimi", result["partial_response"])
        self.assertEqual("partial", result["partial_result_state"])

    async def test_kimi_quota_failure_has_no_partial_and_public_stderr(self) -> None:
        os.environ["AGENT_JOB_KIMI_SEMANTIC"] = "1"
        self.supervisor.quota_routing_enabled = True
        self.supervisor._capacity_health_refresh_at = time.time() + 60
        spec = self.spec("kimi-quota-fail")
        spec["provider"] = "kimi"
        submitted = await self.call(spec)
        result = await self.wait_for(str(submitted["job_id"]), {"failed"})
        self.assertEqual("none", result["partial_result_state"])
        self.assertEqual("rate_limit", result["job"]["failure_kind"])
        self.assertEqual(0.0, self.supervisor._capacity_health_refresh_at)
        self.assertEqual("", result["stdout"])
        self.assertIn("usage limit reached", result["stderr"])
        status = await self.call({"action": "route_status"})
        self.assertEqual("rate_limited", status["provider_health"]["kimi"]["state"])

    async def test_current_kimi_billing_cycle_error_records_rate_limit(self) -> None:
        self.supervisor.quota_routing_enabled = True
        spec = self.spec("kimi-billing-cycle-fail")
        spec["provider"] = "kimi"
        submitted = await self.call(spec)
        result = await self.wait_for(str(submitted["job_id"]), {"failed"})
        self.assertEqual("rate_limit", result["job"]["failure_kind"])
        status = await self.call({"action": "route_status"})
        self.assertEqual("rate_limited", status["provider_health"]["kimi"]["state"])

    async def test_quota_feature_off_preserves_legacy_failure_kind(self) -> None:
        spec = self.spec("kimi-quota-fail")
        spec["provider"] = "kimi"
        submitted = await self.call(spec)
        result = await self.wait_for(str(submitted["job_id"]), {"failed"})
        self.assertEqual("provider_exit", result["job"]["failure_kind"])
        status = await self.call({"action": "route_status"})
        self.assertEqual({}, status["provider_health"])

    async def test_quota_subject_in_stdout_does_not_create_rate_limit(self) -> None:
        self.supervisor.quota_routing_enabled = True
        spec = self.spec("quota-subject-fail")
        spec["provider"] = "kimi"
        submitted = await self.call(spec)
        result = await self.wait_for(str(submitted["job_id"]), {"failed"})
        self.assertEqual("provider_exit", result["job"]["failure_kind"])
        status = await self.call({"action": "route_status"})
        self.assertNotEqual("rate_limited", status["provider_health"]["kimi"]["state"])

    async def test_kimi_stderr_keeps_byte_based_liveness_active(self) -> None:
        os.environ["AGENT_JOB_KIMI_SEMANTIC"] = "1"
        spec = self.spec("kimi-stderr-slow")
        spec["provider"] = "kimi"
        submitted = await self.call(spec)
        job_id = str(submitted["job_id"])
        await self.wait_for(job_id, {"running"})
        await asyncio.sleep(.3)
        self.supervisor.store.update(
            job_id, last_progress_at=time.time() - 31, soft_stall_seconds=30
        )
        result = await self.call({"action": "read", "job_id": job_id})
        self.assertEqual("running", result["job"]["status"])
        self.assertNotEqual("possibly_stalled", result["job"]["status"])
        self.assertEqual("", result["job"]["open_tool"])
        await self.call({"action": "cancel", "job_id": job_id})
        await self.wait_for(job_id, {"cancelled"})

    async def test_kimi_semantic_contract_survives_kill_switch_disable(self) -> None:
        os.environ["AGENT_JOB_KIMI_SEMANTIC"] = "1"
        spec = self.spec("kimi-events")
        spec["provider"] = "kimi"
        submitted = await self.call(spec)
        await self.wait_for(str(submitted["job_id"]), {"completed"})

        os.environ["AGENT_JOB_KIMI_SEMANTIC"] = "0"
        result = await self.call({"action": "read", "job_id": submitted["job_id"]})

        self.assertEqual(1, result["job"]["semantic_stream"])
        self.assertEqual("complete", result["partial_result_state"])
        self.assertEqual("partial answer", result["partial_response"])
        self.assertEqual("", result["output"])
        self.assertEqual("", result["stdout"])

    async def test_kimi_plain_contract_survives_kill_switch_enable(self) -> None:
        spec = self.spec("complete")
        spec["provider"] = "kimi"
        submitted = await self.call(spec)
        await self.wait_for(str(submitted["job_id"]), {"completed"})

        os.environ["AGENT_JOB_KIMI_SEMANTIC"] = "1"
        result = await self.call({"action": "read", "job_id": submitted["job_id"]})

        self.assertEqual(0, result["job"]["semantic_stream"])
        self.assertEqual("unavailable", result["partial_result_state"])
        self.assertIn("first", result["output"])
        self.assertEqual("first\nsecond\n", result["stdout"])

    async def test_claude_events_stream_once_and_reconstruct_result(self) -> None:
        submitted = await self.call(self.spec("claude-events"))
        result = await self.wait_for(str(submitted["job_id"]), {"completed"})
        result = await self.call({
            "action": "read", "job_id": submitted["job_id"], "event_cursor": 0,
        })

        kinds = [event["kind"] for event in result["events"]]
        self.assertIn("waiting", kinds)
        self.assertIn("turn_started", kinds)
        self.assertIn("message_delta", kinds)
        self.assertIn("usage", kinds)
        self.assertEqual(2, kinds.count("message_delta"))
        self.assertEqual("partial answer", result["partial_response"])
        self.assertEqual("complete", result["partial_result_state"])

    async def test_claude_snapshot_collision_recovers_without_terminal_fallback(self) -> None:
        submitted = await self.call(self.spec("claude-snapshot-collision"))
        await self.wait_for(str(submitted["job_id"]), {"completed"})
        result = await self.call({
            "action": "read", "job_id": submitted["job_id"], "event_cursor": 0,
        })

        self.assertEqual("snapshot answer", result["partial_response"])
        self.assertEqual("complete", result["partial_result_state"])
        self.assertEqual("", result["output"])
        self.assertEqual("", result["stdout"])
        self.assertFalse(any(
            event["kind"] == "progress"
            and event["payload"].get("phase") == "terminal_result_recovered"
            for event in result["events"]
        ))

    async def test_claude_result_only_recovers_complete_private_answer(self) -> None:
        submitted = await self.call(self.spec("claude-result-only"))
        await self.wait_for(str(submitted["job_id"]), {"completed"})
        result = await self.call({
            "action": "read", "job_id": submitted["job_id"], "event_cursor": 0,
        })

        self.assertEqual("terminal answer", result["partial_response"])
        self.assertEqual("complete", result["partial_result_state"])
        self.assertEqual("", result["output"])
        self.assertEqual("", result["stdout"])
        self.assertNotIn("private nested text", json.dumps(result["events"]))
        self.assertTrue(any(
            event["kind"] == "progress"
            and event["payload"].get("phase") == "terminal_result_recovered"
            for event in result["events"]
        ))

    async def test_claude_error_result_does_not_recover_private_prose(self) -> None:
        submitted = await self.call(self.spec("claude-result-only-error"))
        await self.wait_for(str(submitted["job_id"]), {"completed"})
        result = await self.call({
            "action": "read", "job_id": submitted["job_id"], "event_cursor": 0,
        })

        self.assertEqual("", result["partial_response"])
        self.assertEqual("none", result["partial_result_state"])
        self.assertEqual(1, result["job"]["provider_result_error"])
        self.assertNotIn("private error prose", json.dumps(result["events"]))
        self.assertNotIn("private nested text", json.dumps(result["events"]))

    async def test_claude_suspected_mixed_loss_marks_partial(self) -> None:
        submitted = await self.call(self.spec("claude-mixed-loss"))
        await self.wait_for(str(submitted["job_id"]), {"completed"})
        result = await self.call({
            "action": "read", "job_id": submitted["job_id"], "event_cursor": 0,
        })

        self.assertEqual("short", result["partial_response"])
        self.assertEqual("partial", result["partial_result_state"])
        self.assertEqual(1, result["job"]["provider_result_error"])
        warnings = [event for event in result["events"] if event["kind"] == "warning"]
        self.assertEqual("suspected_response_loss", warnings[0]["payload"]["subtype"])
        self.assertNotIn("x" * 600, json.dumps(result["events"]))

    async def test_cancelled_claude_job_retains_partial_response(self) -> None:
        submitted = await self.call(self.spec("claude-partial-slow"))
        job_id = str(submitted["job_id"])
        for _ in range(100):
            current = await self.call({
                "action": "read", "job_id": job_id, "event_cursor": 0,
            })
            if current["job"]["has_partial_response"]:
                break
            await asyncio.sleep(.02)
        else:
            self.fail("Claude fixture did not produce its partial response")

        await self.call({"action": "cancel", "job_id": job_id})
        result = await self.wait_for(job_id, {"cancelled"})

        self.assertEqual("recover opus", result["partial_response"])
        self.assertEqual("partial", result["partial_result_state"])

    async def test_claude_tool_activity_omits_input_content(self) -> None:
        submitted = await self.call(self.spec("claude-tool-slow"))
        job_id = str(submitted["job_id"])
        for _ in range(100):
            current = await self.call({
                "action": "read", "job_id": job_id, "event_cursor": 0,
            })
            if current["job"]["activity"] == "tool_running:Read":
                break
            await asyncio.sleep(.02)
        else:
            self.fail("Claude fixture did not expose tool activity")

        self.assertEqual("running", current["job"]["status"])
        self.assertNotIn("secret-input", json.dumps(current["events"]))
        await self.call({"action": "cancel", "job_id": job_id})
        await self.wait_for(job_id, {"cancelled"})

    async def test_claude_declared_wait_escalates_after_soft_stall_threshold(self) -> None:
        submitted = await self.call(self.spec("claude-waiting-slow"))
        job_id = str(submitted["job_id"])
        for _ in range(100):
            current = await self.call({"action": "read", "job_id": job_id})
            if current["job"].get("last_event_kind") == "waiting":
                break
            await asyncio.sleep(.02)
        else:
            self.fail("Claude fixture did not declare a wait")
        self.supervisor.event_summaries[job_id]["last_progress_at"] = time.time() - 600

        current = await self.call({"action": "read", "job_id": job_id})

        self.assertEqual("possibly_stalled", current["job"]["status"])
        self.assertEqual("idle_unknown", current["job"]["activity"])
        await self.call({"action": "cancel", "job_id": job_id})
        await self.wait_for(job_id, {"cancelled"})

    async def test_unmatched_tool_finish_clears_sticky_activity(self) -> None:
        submitted = await self.call(self.spec("slow"))
        job_id = str(submitted["job_id"])
        await self.wait_for(job_id, {"running"})
        self.supervisor._record_event(job_id, "tool_started", {"id": "a", "name": "Read"})

        self.supervisor._record_event(job_id, "tool_finished", {"id": "missing", "name": "Read"})
        current = await self.call({"action": "read", "job_id": job_id})

        self.assertEqual("", current["job"]["open_tool"])
        self.assertEqual(0, current["job"]["open_tool_count"])
        await self.call({"action": "cancel", "job_id": job_id})
        await self.wait_for(job_id, {"cancelled"})

    async def test_claude_error_result_marks_retained_text_partial(self) -> None:
        submitted = await self.call(self.spec("claude-error-zero"))
        result = await self.wait_for(str(submitted["job_id"]), {"completed"})

        self.assertEqual("unfinished", result["partial_response"])
        self.assertEqual("partial", result["partial_result_state"])
        self.assertEqual(1, result["job"]["provider_result_error"])

    async def test_claude_raw_structured_stdout_is_not_returned_to_callers(self) -> None:
        submitted = await self.call(self.spec("claude-events"))
        result = await self.wait_for(str(submitted["job_id"]), {"completed"})
        job = self.supervisor.store.get(str(submitted["job_id"]))

        self.assertEqual("", result["output"])
        self.assertEqual("", result["stdout"])
        self.assertIn('"type": "result"', Path(f"{job['log_path']}.stdout").read_text())

    async def test_claude_normalization_failure_restores_raw_output_recovery(self) -> None:
        original = self.supervisor._record_event

        def fail_progress(job_id, kind, payload=None, *, force=False):
            if kind == "progress":
                raise OSError("injected Claude normalization failure")
            return original(job_id, kind, payload, force=force)

        with patch.object(self.supervisor, "_record_event", side_effect=fail_progress):
            submitted = await self.call(self.spec("claude-events"))
            result = await self.wait_for(str(submitted["job_id"]), {"completed"})

        self.assertEqual(1, result["job"]["semantic_normalization_failed"])
        self.assertIn('"type": "result"', result["stdout"])
        self.assertEqual("unavailable", result["partial_result_state"])
        self.assertIn("Semantic event normalization disabled", result["stderr"])

    async def test_concurrent_claude_tools_keep_oldest_open_until_it_finishes(self) -> None:
        submitted = await self.call(self.spec("slow"))
        job_id = str(submitted["job_id"])
        await self.wait_for(job_id, {"running"})

        self.supervisor._record_event(job_id, "tool_started", {"id": "a", "name": "Glob"})
        first_since = self.supervisor.event_summaries[job_id]["open_tool_since"]
        await asyncio.sleep(.01)
        self.supervisor._record_event(job_id, "tool_started", {"id": "b", "name": "Read"})
        current = await self.call({"action": "read", "job_id": job_id})
        self.assertEqual("tool_running:Glob", current["job"]["activity"])
        self.assertEqual(2, current["job"]["open_tool_count"])
        self.assertEqual(first_since, current["job"]["open_tool_since"])

        self.supervisor._record_event(job_id, "tool_finished", {"id": "b", "name": "Read"})
        current = await self.call({"action": "read", "job_id": job_id})
        self.assertEqual("tool_running:Glob", current["job"]["activity"])
        self.assertEqual(first_since, current["job"]["open_tool_since"])

        self.supervisor._record_event(job_id, "tool_finished", {"id": "a", "name": "Glob"})
        current = await self.call({"action": "read", "job_id": job_id})
        self.assertEqual("", current["job"]["open_tool"])
        await self.call({"action": "cancel", "job_id": job_id})
        await self.wait_for(job_id, {"cancelled"})

    async def test_partial_response_cap_is_reported_as_truncated(self) -> None:
        with patch.object(supervisor_module, "MAX_PARTIAL_RESPONSE_BYTES", 5):
            spec = self.spec("codex-events")
            spec["provider"] = "codex"
            submitted = await self.call(spec)
            result = await self.wait_for(str(submitted["job_id"]), {"completed"})

        self.assertEqual("parti", result["partial_response"])
        self.assertEqual("truncated", result["partial_result_state"])
        self.assertEqual(1, result["job"]["partial_response_truncated"])

    async def test_provider_raw_record_counts_as_codex_progress(self) -> None:
        spec = self.spec("codex-raw-slow")
        spec["provider"] = "codex"
        submitted = await self.call(spec)
        job_id = str(submitted["job_id"])
        for _ in range(100):
            summary = self.supervisor.event_summaries.get(job_id, {})
            if summary.get("last_event_kind") == "provider_raw":
                break
            await asyncio.sleep(.02)
        else:
            self.fail("Codex fixture did not emit provider_raw")
        self.supervisor.store.update(
            job_id, started_at=time.time() - 60, last_output_at=time.time() - 60,
            soft_stall_seconds=30,
        )

        current = await self.call({"action": "read", "job_id": job_id})

        self.assertEqual("running", current["job"]["status"])
        self.assertLess(current["job"]["seconds_without_progress"], 3)
        await self.call({"action": "cancel", "job_id": job_id})
        await self.wait_for(job_id, {"cancelled"})

    async def test_cancelled_codex_job_retains_partial_response(self) -> None:
        spec = self.spec("codex-partial-slow")
        spec["provider"] = "codex"
        submitted = await self.call(spec)
        for _ in range(100):
            current = await self.call({
                "action": "read", "job_id": submitted["job_id"], "event_cursor": 0,
            })
            if current["job"]["has_partial_response"]:
                break
            await asyncio.sleep(.02)
        else:
            self.fail("Codex fixture did not produce its partial response")

        await self.call({"action": "cancel", "job_id": submitted["job_id"]})
        result = await self.wait_for(str(submitted["job_id"]), {"cancelled"})

        self.assertEqual("recover me", result["partial_response"])
        self.assertEqual("partial", result["partial_result_state"])

    async def test_open_codex_tool_is_activity_not_stall(self) -> None:
        spec = self.spec("codex-tool-slow")
        spec["provider"] = "codex"
        submitted = await self.call(spec)
        for _ in range(150):
            current = await self.call({"action": "read", "job_id": submitted["job_id"]})
            if str(current["job"]["activity"]).startswith("tool_running:"):
                break
            await asyncio.sleep(.02)
        else:
            self.fail("Codex fixture did not expose tool activity")
        self.supervisor.event_summaries[str(submitted["job_id"])]["open_tool_since"] = time.time() - 600
        current = await self.call({"action": "read", "job_id": submitted["job_id"]})
        self.assertEqual("running", current["job"]["status"])
        self.assertEqual("tool_running:command", current["job"]["activity"])
        self.assertGreaterEqual(current["job"]["open_tool_seconds"], 600)
        await self.call({"action": "cancel", "job_id": submitted["job_id"]})
        await self.wait_for(str(submitted["job_id"]), {"cancelled"})

    async def test_event_long_poll_wakes_with_new_normalized_event(self) -> None:
        spec = self.spec("codex-tool-slow")
        spec["provider"] = "codex"
        submitted = await self.call(spec)
        await self.wait_for(str(submitted["job_id"]), {"running"})
        current = await self.call({
            "action": "read", "job_id": submitted["job_id"], "event_cursor": 0,
        })
        seen = [event["kind"] for event in current["events"]]
        cursor = current["event_cursor"]
        output_cursor = current["cursor"]
        while "tool_started" not in seen:
            current = await self.call({
                "action": "read", "job_id": submitted["job_id"],
                "cursor": output_cursor, "event_cursor": cursor, "wait_seconds": 3,
            })
            seen.extend(event["kind"] for event in current["events"])
            cursor = current["event_cursor"]
            output_cursor = current["cursor"]
        self.assertIn("tool_started", seen)
        await self.call({"action": "cancel", "job_id": submitted["job_id"]})
        await self.wait_for(str(submitted["job_id"]), {"cancelled"})

    async def test_codex_stderr_bytes_do_not_mask_semantic_silence(self) -> None:
        spec = self.spec("codex-tool-slow")
        spec["provider"] = "codex"
        submitted = await self.call(spec)
        await self.wait_for(str(submitted["job_id"]), {"running"})
        job_id = str(submitted["job_id"])
        for _ in range(100):
            if self.supervisor.event_summaries.get(job_id, {}).get("open_tool"):
                break
            await asyncio.sleep(.02)
        else:
            self.fail("Codex fixture did not open its tool")
        summary = self.supervisor.event_summaries[job_id]
        summary["last_progress_at"] = time.time() - 31
        summary["open_tool"] = ""
        summary["open_tool_since"] = None
        await self.supervisor._append_log(job_id, "stderr", b"warning spinner\n")

        current = await self.call({"action": "read", "job_id": job_id})

        self.assertEqual("possibly_stalled", current["job"]["status"])
        self.assertEqual("idle_unknown", current["job"]["activity"])
        self.assertLess(current["job"]["seconds_without_output"], 3)
        await self.call({"action": "cancel", "job_id": job_id})
        await self.wait_for(job_id, {"cancelled"})

    async def test_provider_queue_is_machine_wide_within_daemon(self) -> None:
        first = await self.call(self.spec("slow"))
        second = await self.call(self.spec("complete"))
        await self.wait_for(str(first["job_id"]), {"running"})
        queued = await self.call({"action": "read", "job_id": second["job_id"]})
        self.assertEqual("queued", queued["job"]["status"])
        await self.call({"action": "cancel", "job_id": first["job_id"]})
        await self.wait_for(str(first["job_id"]), {"cancelled"})
        await self.wait_for(str(second["job_id"]), {"completed"})

    async def test_list_filters_owner_before_applying_limit(self) -> None:
        wanted = self.spec("complete")
        wanted["owner"] = "wanted:checkpoint"
        first = await self.call(wanted)
        await self.wait_for(str(first["job_id"]), {"completed"})
        other = self.spec("complete")
        other["owner"] = "other:checkpoint"
        second = await self.call(other)
        await self.wait_for(str(second["job_id"]), {"completed"})
        result = await self.call({"action": "list", "owner": "wanted", "limit": 1})
        self.assertEqual([first["job_id"]], [job["job_id"] for job in result["jobs"]])

    async def test_stalled_filter_is_applied_before_limit(self) -> None:
        self.supervisor.provider_limits["claude"] = 2
        stalled = await self.call(self.spec("slow"))
        await self.wait_for(str(stalled["job_id"]), {"running"})
        for _ in range(100):
            stored = self.supervisor.store.get(str(stalled["job_id"]))
            if stored["last_output_at"] > stored["started_at"]:
                break
            await asyncio.sleep(.01)
        self.supervisor.store.update(
            str(stalled["job_id"]), last_output_at=time.time() - 31, soft_stall_seconds=30
        )
        self.supervisor.event_summaries[str(stalled["job_id"])]["last_progress_at"] = time.time() - 31
        active = await self.call(self.spec("slow"))
        await self.wait_for(str(active["job_id"]), {"running"})
        result = await self.call({"action": "list", "status": "possibly_stalled", "limit": 1})
        self.assertEqual([stalled["job_id"]], [job["job_id"] for job in result["jobs"]])
        for job in (stalled, active):
            await self.call({"action": "cancel", "job_id": job["job_id"]})
            await self.wait_for(str(job["job_id"]), {"cancelled"})

    async def test_production_concurrency_never_launches_one_job_twice(self) -> None:
        self.supervisor.provider_limits["claude"] = 2
        submitted = await self.call(self.spec("slow"))
        await self.wait_for(str(submitted["job_id"]), {"running"})
        await asyncio.sleep(.75)
        self.assertEqual(1, self.launch_counts[str(submitted["job_id"])])
        await self.call({"action": "cancel", "job_id": submitted["job_id"]})
        await self.wait_for(str(submitted["job_id"]), {"cancelled"})

    async def test_dynamic_capacity_reduces_pressure_and_pauses_cooldown(self) -> None:
        self.supervisor.dynamic_concurrency_enabled = True
        self.supervisor.provider_limits["claude"] = 3
        self.assertEqual(2, self.supervisor._effective_provider_limits({
            "claude": {"state": "pressured"},
        })["claude"])
        self.assertEqual(3, self.supervisor._effective_provider_limits({
            "claude": {"state": "exhausted"},
        })["claude"])

        self.supervisor._capacity_health = {"claude": {"state": "rate_limited"}}
        self.supervisor._capacity_health_refresh_at = time.time() + 60
        submitted = await self.call(self.spec("complete"))
        await asyncio.sleep(.4)
        queued = await self.call({"action": "read", "job_id": submitted["job_id"]})
        self.assertEqual("queued", queued["job"]["status"])

        self.supervisor._capacity_health = {"claude": {"state": "available"}}
        result = await self.wait_for(str(submitted["job_id"]), {"completed"}, timeout=5)
        self.assertEqual("completed", result["job"]["status"])

    async def test_capacity_drop_does_not_cancel_running_jobs(self) -> None:
        self.supervisor.dynamic_concurrency_enabled = True
        self.supervisor.provider_limits["claude"] = 2
        first = await self.call(self.spec("delayed"))
        second = await self.call(self.spec("delayed"))
        await self.wait_for(str(first["job_id"]), {"running"})
        await self.wait_for(str(second["job_id"]), {"running"})
        self.supervisor._capacity_health = {"claude": {"state": "rate_limited"}}
        self.supervisor._capacity_health_refresh_at = time.time() + 60
        for submitted in (first, second):
            result = await self.wait_for(str(submitted["job_id"]), {"completed"}, timeout=5)
            self.assertEqual("completed", result["job"]["status"])

    async def test_pressured_scheduler_enforces_reduced_slot_count(self) -> None:
        self.supervisor.dynamic_concurrency_enabled = True
        self.supervisor.provider_limits["claude"] = 3
        self.supervisor._capacity_health = {"claude": {"state": "pressured"}}
        self.supervisor._capacity_health_refresh_at = time.time() + 60
        jobs = [await self.call(self.spec("delayed")) for _ in range(3)]
        await self.wait_for(str(jobs[0]["job_id"]), {"running"})
        await self.wait_for(str(jobs[1]["job_id"]), {"running"})
        third = await self.call({"action": "read", "job_id": jobs[2]["job_id"]})
        self.assertEqual("queued", third["job"]["status"])
        for submitted in jobs:
            await self.wait_for(str(submitted["job_id"]), {"completed"}, timeout=6)

    async def test_expired_cooldown_automatically_restores_scheduler_capacity(self) -> None:
        self.supervisor.quota_routing_enabled = True
        self.supervisor.dynamic_concurrency_enabled = True
        self.supervisor.store.record_provider_rate_limit("claude", time.time() + 60, "test")
        self.supervisor._capacity_health_refresh_at = 0
        submitted = await self.call(self.spec("complete"))
        await asyncio.sleep(.4)
        queued = await self.call({"action": "read", "job_id": submitted["job_id"]})
        self.assertEqual("queued", queued["job"]["status"])
        self.supervisor.store.db.execute(
            "UPDATE provider_health SET cooldown_until = ? WHERE provider = 'claude'",
            (time.time() - 1,),
        )
        self.supervisor.store.db.commit()
        self.supervisor._capacity_health_refresh_at = 0
        result = await self.wait_for(str(submitted["job_id"]), {"completed"}, timeout=5)
        self.assertEqual("completed", result["job"]["status"])

    async def test_stale_health_keeps_configured_capacity(self) -> None:
        self.supervisor.dynamic_concurrency_enabled = True
        self.supervisor.provider_limits["claude"] = 3
        self.assertEqual(3, self.supervisor._effective_provider_limits({
            "claude": {"state": "stale"},
        })["claude"])

    async def test_exhaustion_does_not_hold_submitted_job_without_dynamic_concurrency(self) -> None:
        self.supervisor.quota_routing_enabled = True
        self.supervisor.dynamic_concurrency_enabled = False
        self.supervisor.provider_limits["claude"] = 1
        now = time.time()
        quota_dir = Path(os.environ["AGENT_JOB_QUOTA_HISTORY_DIR"])
        quota_dir.mkdir(parents=True)
        iso = lambda value: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))
        (quota_dir / "claude.json").write_text(json.dumps({
            "preferredAccountKey": "test",
            "accounts": {"test": [{
                "name": "weekly", "windowMinutes": 10080,
                "entries": [{
                    "capturedAt": iso(now), "resetsAt": iso(now + 3600),
                    "usedPercent": 99,
                }],
            }]},
        }), encoding="utf-8")
        self.supervisor.store.create({
            "provider": "claude", "model": "opus", "requested_model": "opus",
            "mode": "readonly", "workdir": str(self.workdir), "prompt": "complete",
            "owner": "", "message": "", "timeout_seconds": 60,
            "soft_stall_seconds": 30, "max_turns": 0, "execution_backend": "native",
            "semantic_stream": 0, "idempotency_key": "", "request_hash": "queued",
        }, "pre-exhaustion-job", Path(self.temp.name) / "queued.log")
        await asyncio.sleep(.4)
        result = await self.wait_for("pre-exhaustion-job", {"completed"})
        self.assertEqual("completed", result["job"]["status"])

    async def test_route_status_reports_dynamic_slots_and_native_feedback_gate(self) -> None:
        self.supervisor.quota_routing_enabled = True
        self.supervisor.dynamic_concurrency_enabled = True
        self.supervisor.provider_limits = {"claude": 3, "kimi": 3, "codex": 3}
        status = await self.call({"action": "route_status"})
        self.assertEqual({"claude": 3, "kimi": 3, "codex": 3}, status["configured_provider_slots"])
        self.assertEqual("fixed_advisory", status["native_capacity_mode"])
        self.assertFalse(status["native_feedback_gate_met"])

        self.supervisor.routing_mode = "codex_canary"
        decision = await self.call(self.codex_native_route("gate-ready"))
        await self.call({
            "action": "route_feedback", "decision_id": decision["decision_id"],
            "session_id": "gate-ready", "outcome": "completed",
        })
        status = await self.call({"action": "route_status"})
        self.assertTrue(status["native_feedback_gate_met"])

    async def test_scheduler_recovers_after_iteration_error(self) -> None:
        original_queued = self.supervisor.store.queued
        calls = 0

        def fail_once() -> list[dict[str, object]]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("synthetic scheduler failure")
            return original_queued()

        self.supervisor.store.queued = fail_once  # type: ignore[method-assign]
        submitted = await self.call(self.spec("complete"))
        result = await self.wait_for(str(submitted["job_id"]), {"completed"}, timeout=5)
        self.assertEqual("completed", result["job"]["status"])

    async def test_queue_timeout_expires_without_waiting_for_a_provider_slot(self) -> None:
        first = await self.call(self.spec("slow"))
        second = await self.call(self.spec("complete"))
        await self.wait_for(str(first["job_id"]), {"running"})
        self.supervisor.store.update(
            str(second["job_id"]), created_at=time.time() - 31,
            queue_timeout_seconds=30, run_timeout_seconds=30,
        )
        result = await self.wait_for(str(second["job_id"]), {"failed"})
        self.assertEqual("queue_timeout", result["job"]["failure_kind"])
        await self.call({"action": "cancel", "job_id": first["job_id"]})
        await self.wait_for(str(first["job_id"]), {"cancelled"})

    async def test_run_timeout_starts_when_provider_launches(self) -> None:
        first = await self.call(self.spec("slow"))
        second = await self.call(self.spec("slow"))
        await self.wait_for(str(first["job_id"]), {"running"})
        self.supervisor.store.update(
            str(second["job_id"]), created_at=time.time() - 20,
            queue_timeout_seconds=60, run_timeout_seconds=1, timeout_seconds=1,
        )
        await self.call({"action": "cancel", "job_id": first["job_id"]})
        await self.wait_for(str(first["job_id"]), {"cancelled"})
        started = await self.wait_for(str(second["job_id"]), {"running", "failed"})
        self.assertIsNotNone(started["job"]["started_at"])
        result = await self.wait_for(str(second["job_id"]), {"failed"})
        self.assertEqual("timeout", result["job"]["failure_kind"])
        self.assertGreaterEqual(
            result["job"]["finished_at"] - result["job"]["started_at"], .9
        )

    async def test_legacy_job_keeps_submit_relative_shared_deadline(self) -> None:
        spec = self.spec("complete")
        spec.update({"idempotency_key": "legacy-deadline", "request_hash": "legacy"})
        job_id = "legacy-deadline-job"
        self.supervisor.store.create(spec, job_id, self.supervisor.log_dir / f"{job_id}.log")
        self.supervisor.store.update(job_id, created_at=time.time() - 31, timeout_seconds=30)
        result = await self.wait_for(job_id, {"failed"})
        self.assertEqual("queue_timeout", result["job"]["failure_kind"])
        self.assertEqual("legacy_shared", result["job"]["timeout_semantics"])

    async def test_cancelled_queued_job_never_launches(self) -> None:
        first = await self.call(self.spec("slow"))
        second = await self.call(self.spec("complete"))
        await self.wait_for(str(first["job_id"]), {"running"})
        cancelled = await self.call({"action": "cancel", "job_id": second["job_id"]})
        self.assertEqual("cancelled", cancelled["status"])
        await self.call({"action": "cancel", "job_id": first["job_id"]})
        await self.wait_for(str(first["job_id"]), {"cancelled"})
        await asyncio.sleep(.5)
        result = await self.call({"action": "read", "job_id": second["job_id"]})
        self.assertEqual("cancelled", result["job"]["status"])
        self.assertEqual("", result.get("stdout", ""))
        self.assertNotIn(str(second["job_id"]), self.supervisor.change_events)

    async def test_recursive_submission_is_rejected(self) -> None:
        spec = self.spec("complete")
        spec["caller_depth"] = 1
        with self.assertRaisesRegex(RuntimeError, "Recursive"):
            await self.call(spec)

    async def test_kimi_defaults_to_k3_and_records_blank_request(self) -> None:
        spec = self.spec("complete")
        spec.update(provider="kimi", model="")
        submitted = await self.call(spec)
        self.assertEqual("kimi-code/k3", submitted["model"])
        self.assertEqual("", submitted["requested_model"])

    async def test_kimi_default_sentinels_and_environment_override(self) -> None:
        for requested in ("auto", "DEFAULT", "kimi", "   "):
            spec = self.spec("complete")
            spec.update(provider="kimi", model=requested)
            with patch.dict(
                os.environ,
                {"AGENT_JOB_KIMI_DEFAULT_MODEL": "kimi-code/k3-256k"},
                clear=False,
            ):
                submitted = await self.call(spec)
            self.assertEqual("kimi-code/k3-256k", submitted["model"])
            self.assertEqual(requested.strip(), submitted["requested_model"])

    async def test_kimi_legacy_alias_normalizes_to_k27(self) -> None:
        for requested in ("kimi-k2.5", "kimi-k2.6", "k2.6", "old-kimi-alias"):
            spec = self.spec("complete")
            spec.update(provider="kimi", model=requested)
            submitted = await self.call(spec)
            self.assertEqual("kimi-code/kimi-for-coding", submitted["model"])
            self.assertEqual(requested, submitted["requested_model"])
            self.assertIn("normalized", submitted["message"])

    async def test_kimi_supported_aliases_are_canonicalized(self) -> None:
        cases = {
            "k3": "kimi-code/k3",
            "kimi-code/k3": "kimi-code/k3",
            "k3-256k": "kimi-code/k3-256k",
            "kimi-code/kimi-for-coding": "kimi-code/kimi-for-coding",
            "kimi-for-coding-highspeed": "kimi-code/kimi-for-coding-highspeed",
        }
        for requested, expected in cases.items():
            spec = self.spec("complete")
            spec.update(provider="kimi", model=requested)
            submitted = await self.call(spec)
            self.assertEqual(expected, submitted["model"])
            self.assertEqual(requested, submitted["requested_model"])
            self.assertEqual("", submitted["message"])

    async def test_kimi_aliases_are_case_insensitive_and_malformed_values_fail(self) -> None:
        spec = self.spec("complete")
        spec.update(provider="kimi", model="Kimi-Code/K3")
        submitted = await self.call(spec)
        self.assertEqual("kimi-code/k3", submitted["model"])

        malformed = self.spec("complete")
        malformed.update(provider="kimi", model="kimi k2.6")
        with self.assertRaisesRegex(RuntimeError, "unsupported characters"):
            await self.call(malformed)

        defaulted = self.spec("complete")
        defaulted.update(provider="kimi", model="")
        with patch.dict(
            os.environ, {"AGENT_JOB_KIMI_DEFAULT_MODEL": "invalid model"}, clear=False
        ):
            with self.assertRaisesRegex(RuntimeError, "Effective model"):
                await self.call(defaulted)

    async def test_idempotency_distinguishes_requested_kimi_aliases(self) -> None:
        first = self.spec("complete")
        first.update(
            provider="kimi", model="kimi-k2.5", idempotency_key="alias-audit"
        )
        await self.call(first)
        second = first.copy()
        second["model"] = "kimi-k2.6"
        with self.assertRaisesRegex(RuntimeError, "different job specification"):
            await self.call(second)

    async def test_non_kimi_provider_still_requires_a_model(self) -> None:
        spec = self.spec("complete")
        spec["model"] = ""
        with self.assertRaisesRegex(RuntimeError, "Model is required"):
            await self.call(spec)

    async def test_relative_workdir_is_rejected_by_daemon(self) -> None:
        spec = self.spec("complete")
        spec["workdir"] = "."
        with self.assertRaisesRegex(RuntimeError, "absolute path"):
            await self.call(spec)

    async def test_missing_allowed_roots_fail_closed(self) -> None:
        os.environ["AGENT_JOB_ALLOWED_ROOTS"] = str(Path(self.temp.name) / "missing")
        with self.assertRaisesRegex(RuntimeError, "fail-open"):
            await self.call(self.spec("complete"))
        os.environ["AGENT_JOB_ALLOWED_ROOTS"] = str(Path(self.temp.name))

    async def test_implementation_requires_capability(self) -> None:
        spec = self.spec("complete")
        spec["mode"] = "implement"
        with self.assertRaisesRegex(RuntimeError, "Invalid implementation capability"):
            await self.call(spec)
        spec["implement_capability"] = "test-capability"
        submitted = await self.call(spec)
        result = await self.wait_for(str(submitted["job_id"]), {"completed"})
        self.assertEqual("completed", result["job"]["status"])

    async def test_large_valid_prompt_crosses_socket_transport(self) -> None:
        spec = self.spec(("line with a quote: \"value\"\n" * 18_000)[:500_000])
        submitted = await self.call(spec)
        result = await self.wait_for(str(submitted["job_id"]), {"completed"})
        self.assertEqual(0, result["job"]["exit_code"])

    async def test_argv_prompt_with_unclosed_quotes_does_not_break_launch_identity(self) -> None:
        original_builder = self.supervisor.command_builder

        def argv_prompt_command(job: dict[str, object]) -> tuple[list[str], None, dict[str, str]]:
            return [
                sys.executable,
                "-c",
                "import time; print('ok', flush=True); time.sleep(.3)",
                str(job["prompt"]),
            ], None, os.environ.copy()

        self.supervisor.command_builder = argv_prompt_command
        try:
            spec = self.spec("Review the user's intent")
            spec["provider"] = "kimi"
            submitted = await self.call(spec)
            result = await self.wait_for(str(submitted["job_id"]), {"completed", "failed"})
        finally:
            self.supervisor.command_builder = original_builder

        self.assertEqual("completed", result["job"]["status"])
        live_binary = Path(str(result["job"]["binary_path"]))
        self.assertTrue(live_binary.is_absolute())
        self.assertIn("python", live_binary.name.lower())

    async def test_prompt_over_four_mib_is_rejected(self) -> None:
        spec = self.spec("x" * (supervisor_module.MAX_PROMPT_BYTES + 1))
        with self.assertRaisesRegex(RuntimeError, "Prompt must contain"):
            await self.call(spec)

    async def test_duplicate_idempotency_key_returns_original_job(self) -> None:
        spec = self.spec("complete")
        spec["idempotency_key"] = "same-request"
        first = await self.call(spec)
        second = await self.call(spec)
        self.assertEqual(first["job_id"], second["job_id"])
        jobs = await self.call({"action": "list", "limit": 50})
        matching = [job for job in jobs["jobs"] if job.get("idempotency_key") == "same-request"]
        self.assertEqual(1, len(matching))

    async def test_running_job_is_reconciled_after_restart(self) -> None:
        spec = self.spec("slow")
        spec["owner"] = "codex:restart-test"
        submitted = await self.call(spec)
        await self.wait_for(str(submitted["job_id"]), {"running"})
        job = self.supervisor.store.get(str(submitted["job_id"]))
        self.supervisor.store.update(str(submitted["job_id"]), pgid=None)
        reconciled = self.supervisor.store.reconcile()
        self.assertTrue(any(item["job_id"] == submitted["job_id"] for item in reconciled))
        current = self.supervisor.store.get(str(submitted["job_id"]))
        self.assertEqual("interrupted", current["status"])
        inbox = await self.call({"action": "inbox", "owner": "codex:restart-test"})
        self.assertEqual([submitted["job_id"]], [item["job"]["job_id"] for item in inbox["deliveries"]])
        # Restore running state so normal teardown owns and terminates the live test process.
        self.supervisor.store.update(str(submitted["job_id"]), status="running", pgid=job["pgid"])
        await self.call({"action": "cancel", "job_id": submitted["job_id"]})
        await self.wait_for(str(submitted["job_id"]), {"cancelled"})

    async def test_prune_removes_delivery_with_terminal_job(self) -> None:
        spec = self.spec("complete")
        spec["owner"] = "codex:prune-test"
        submitted = await self.call(spec)
        await self.wait_for(str(submitted["job_id"]), {"completed"})
        job = self.supervisor.store.get(str(submitted["job_id"]))
        event_path = Path(f"{job['log_path']}.events.jsonl")
        self.assertTrue(event_path.is_file())
        self.supervisor.store.update(str(submitted["job_id"]), finished_at=1)

        for base in self.supervisor.store.prune(cutoff=2):
            for suffix in ("", ".stdout", ".stderr", ".events.jsonl", ".partial.txt"):
                Path(f"{base}{suffix}").unlink(missing_ok=True)

        inbox = await self.call({"action": "inbox", "owner": "codex:prune-test"})
        self.assertEqual([], inbox["deliveries"])
        self.assertFalse(event_path.exists())
        with self.assertRaisesRegex(RuntimeError, "Unknown job id"):
            await self.call({"action": "read", "job_id": submitted["job_id"]})

    async def test_restart_cleanup_refuses_mismatched_process_identity(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
        )
        try:
            spec = self.spec("complete")
            spec.update({
                "soft_stall_seconds": 30,
                "idempotency_key": "identity-mismatch",
                "request_hash": "hash",
            })
            job_id = "identity-mismatch-job"
            self.supervisor.store.create(spec, job_id, self.supervisor.log_dir / f"{job_id}.log")
            self.supervisor.store.update(
                job_id, status="running", pid=process.pid, pgid=process.pid,
                binary_path=sys.executable, process_start="definitely-not-the-start-time",
            )
            await self.supervisor._cleanup_interrupted()
            self.assertIsNone(process.poll())
            job = self.supervisor.store.get(job_id)
            self.assertIn("identity did not match", job["message"])
        finally:
            process.terminate()
            process.wait(timeout=5)

    async def test_restart_cleanup_terminates_exact_recorded_process(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
        )
        spec = self.spec("complete")
        spec.update({
            "soft_stall_seconds": 30,
            "idempotency_key": "identity-match",
            "request_hash": "hash",
        })
        job_id = "identity-match-job"
        self.supervisor.store.create(spec, job_id, self.supervisor.log_dir / f"{job_id}.log")
        process_start = await self.supervisor._ps_field(process.pid, "lstart")
        live_executable = await self.supervisor._ps_field(process.pid, "comm")
        live_binary = str(Path(live_executable).resolve())
        self.supervisor.store.update(
            job_id, status="running", pid=process.pid, pgid=process.pid,
            binary_path=live_binary, process_start=process_start,
        )
        await self.supervisor._cleanup_interrupted()
        process.wait(timeout=5)
        self.assertIsNotNone(process.returncode)
        self.assertEqual("interrupted", self.supervisor.store.get(job_id)["status"])

    async def test_restart_cleanup_ignores_quotes_in_process_arguments(self) -> None:
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
                "Review the user's intent",
            ],
            start_new_session=True,
        )
        try:
            spec = self.spec("complete")
            spec.update({
                "soft_stall_seconds": 30,
                "idempotency_key": "identity-quoted-argument",
                "request_hash": "hash",
            })
            job_id = "identity-quoted-argument-job"
            self.supervisor.store.create(spec, job_id, self.supervisor.log_dir / f"{job_id}.log")
            process_start = await self.supervisor._ps_field(process.pid, "lstart")
            live_executable = await self.supervisor._ps_field(process.pid, "comm")
            self.supervisor.store.update(
                job_id, status="running", pid=process.pid, pgid=process.pid,
                binary_path=str(Path(live_executable).resolve()), process_start=process_start,
            )

            await self.supervisor._cleanup_interrupted()
            process.wait(timeout=5)

            self.assertIsNotNone(process.returncode)
            self.assertEqual("interrupted", self.supervisor.store.get(job_id)["status"])
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)

    async def test_running_job_hard_deadline_terminates_process(self) -> None:
        spec = self.spec("slow")
        spec.update({
            "timeout_seconds": 1,
            "queue_timeout_seconds": 30,
            "run_timeout_seconds": 1,
            "soft_stall_seconds": 1,
            "idempotency_key": "running-timeout",
            "request_hash": "hash",
        })
        job_id = "running-timeout-job"
        self.supervisor.store.create(spec, job_id, self.supervisor.log_dir / f"{job_id}.log")
        result = await self.wait_for(job_id, {"failed"}, timeout=5)
        self.assertEqual("timeout", result["job"]["failure_kind"])

    async def test_provider_env_is_scoped_and_command_builder_is_real(self) -> None:
        profile = Path(self.temp.name) / "provider.env"
        profile.write_text("MOONSHOT_API_KEY=kimi-secret\nANTHROPIC_API_KEY=claude-secret\n")
        old = os.environ.get("AGENT_JOB_PROFILE_ENV")
        old_cao_token = os.environ.get("AGENT_JOB_CAO_TOKEN")
        os.environ["AGENT_JOB_PROFILE_ENV"] = str(profile)
        try:
            base = {
                "provider": "kimi", "model": "kimi-code/k3", "mode": "readonly",
                "prompt": "review", "max_turns": 2, "workdir": str(self.workdir),
                "semantic_stream": 0,
            }
            argv, stdin_text, env = self.supervisor._build_command(base)
            self.assertIn("--agent-file", argv)
            self.assertNotIn("--output-format", argv)
            self.assertEqual("kimi-secret", env["MOONSHOT_API_KEY"])
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertIsNone(stdin_text)
            with patch.dict(os.environ, {"AGENT_JOB_KIMI_SEMANTIC": "1"}):
                base["semantic_stream"] = 1
                argv, _, _ = self.supervisor._build_command(base)
            self.assertIn("--output-format", argv)
            self.assertIn("stream-json", argv)
            base.update(provider="claude", model="opus")
            argv, stdin_text, env = self.supervisor._build_command(base)
            self.assertEqual("claude-secret", env["ANTHROPIC_API_KEY"])
            self.assertNotIn("MOONSHOT_API_KEY", env)
            self.assertEqual("review", stdin_text)
            self.assertIn("--safe-mode", argv)
            self.assertIn("stream-json", argv)
            self.assertIn("--include-partial-messages", argv)
            self.assertIn("--verbose", argv)
            self.assertIn("--no-session-persistence", argv)
            self.assertIn("--max-turns", argv)
            base["max_turns"] = 0
            argv, _, _ = self.supervisor._build_command(base)
            self.assertNotIn("--max-turns", argv)
            base.update(provider="codex", model="gpt-5.6-codex")
            os.environ["AGENT_JOB_CAO_TOKEN"] = "must-not-reach-native"
            argv, stdin_text, env = self.supervisor._build_command(base)
            self.assertIn("--ignore-user-config", argv)
            self.assertEqual("review", stdin_text)
            self.assertNotIn("AGENT_JOB_CAO_TOKEN", env)
        finally:
            if old_cao_token is None:
                os.environ.pop("AGENT_JOB_CAO_TOKEN", None)
            else:
                os.environ["AGENT_JOB_CAO_TOKEN"] = old_cao_token
            if old is None:
                os.environ.pop("AGENT_JOB_PROFILE_ENV", None)
            else:
                os.environ["AGENT_JOB_PROFILE_ENV"] = old

    async def test_cao_backend_uses_bridge_without_native_provider_binary(self) -> None:
        profile = Path(self.temp.name) / "provider.env"
        profile.write_text("ANTHROPIC_API_KEY=must-not-reach-bridge\n")
        job = {
            "job_id": "bridge-job",
            "provider": "claude",
            "model": "opus",
            "mode": "readonly",
            "prompt": "review",
            "max_turns": 0,
            "workdir": str(self.workdir),
            "execution_backend": "cao",
            "created_at": 1000.0,
            "timeout_seconds": 1800,
        }
        with patch.dict(
            os.environ,
            {
                "AGENT_JOB_EXECUTION_BACKEND": "cao",
                "AGENT_JOB_CAO_URL": "http://127.0.0.1:9889",
                "AGENT_JOB_PROFILE_ENV": str(profile),
            },
        ):
            argv, stdin_text, env = self.supervisor._build_command(job)

        self.assertEqual(sys.executable, argv[0])
        self.assertTrue(argv[1].endswith("cao_job_bridge.py"))
        self.assertIn("bridge-job", argv)
        self.assertEqual("review", stdin_text)
        self.assertEqual("http://127.0.0.1:9889", env["AGENT_JOB_CAO_URL"])
        self.assertEqual("2800.0", env["AGENT_JOB_DEADLINE_EPOCH"])
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    async def test_route_decide_persists_shadow_decision_without_creating_job(self) -> None:
        decision = await self.call({
            "action": "route_decide",
            "protocol_version": 1,
            "caller_provider": "codex",
            "surface": "codex",
            "capability": "planning",
            "complexity": "deep",
            "risk": "medium",
            "scope": "repo",
            "duration": "long",
            "durability": "durable",
            "parallelizable": False,
            "surface_capabilities": {"native_subagents": True},
            "owner": "test:shadow",
        })
        self.assertEqual("shadow", decision["mode"])
        self.assertFalse(decision["enforced"])
        self.assertEqual("agent_jobs", decision["lane"])
        self.assertEqual("claude", decision["provider"])
        self.assertEqual([], self.supervisor.store.list())
        row = self.supervisor.store.db.execute(
            "SELECT * FROM route_decisions WHERE decision_id = ?",
            (decision["decision_id"],),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual("test:shadow", row["owner"])

    async def test_surface_canary_enforces_v2_and_degrades_missing_transport(self) -> None:
        self.supervisor.routing_mode = "surface_canary"
        payload = {
            "action": "route_decide", "protocol_version": 2,
            "caller_provider": "claude", "surface": "claude-code",
            "capability": "planning", "session_id": "claude-session",
        }

        degraded = await self.call(payload)
        durable = await self.call({
            **payload,
            "surface_capabilities": {"durable_agent_jobs": True},
        })

        self.assertTrue(degraded["enforced"])
        self.assertEqual("direct", degraded["lane"])
        self.assertEqual("agent_jobs", degraded["degraded_from_lane"])
        self.assertTrue(durable["enforced"])
        self.assertEqual("agent_jobs", durable["lane"])
        self.assertEqual("codex", durable["provider"])

    def durable_planning_route(self, session_id: str) -> dict[str, object]:
        return {
            "action": "route_decide", "protocol_version": 2,
            "caller_provider": "codex", "surface": "codex",
            "capability": "planning", "complexity": "deep", "risk": "medium",
            "scope": "repo", "duration": "long", "durability": "durable",
            "parallelizable": False,
            "surface_capabilities": {"durable_agent_jobs": True},
            "session_id": session_id,
        }

    async def escalate_route(
        self, parent_id: str, session_id: str, evidence: str = "provider exited"
    ) -> dict[str, object]:
        return await self.call({
            **self.durable_planning_route(session_id),
            "previous_decision_id": parent_id,
            "escalation_reason": "provider_failure",
            "escalation_evidence": evidence,
        })

    async def test_one_hop_escalation_is_atomic_idempotent_and_persisted(self) -> None:
        self.supervisor.routing_mode = "surface_canary"
        parent = await self.call(self.durable_planning_route("escalation-session"))
        await self.call({
            "action": "route_feedback", "decision_id": parent["decision_id"],
            "session_id": "escalation-session", "outcome": "escalated",
        })

        evidence = "provider exited; api_key=must-not-persist"
        child = await self.escalate_route(
            str(parent["decision_id"]), "escalation-session", evidence
        )
        retry = await self.escalate_route(
            str(parent["decision_id"]), "escalation-session", evidence
        )

        self.assertEqual("claude", parent["provider"])
        self.assertEqual("kimi", child["provider"])
        self.assertEqual("kimi-code/k3", child["model_alias"])
        self.assertEqual("", child["fallback_provider"])
        self.assertEqual(parent["decision_id"], child["parent_decision_id"])
        self.assertEqual(1, child["escalation_hop"])
        self.assertEqual(child["decision_id"], retry["decision_id"])
        self.assertTrue(retry["idempotent"])
        row = self.supervisor.store.db.execute(
            "SELECT * FROM route_decisions WHERE decision_id = ?", (child["decision_id"],)
        ).fetchone()
        self.assertEqual("provider_failure", row["escalation_reason"])
        self.assertEqual("provider exited; api_key=[REDACTED]", row["escalation_evidence"])
        status = await self.call({"action": "route_status"})
        self.assertEqual({"provider_failure": 1}, status["one_hop_escalations"])

        with self.assertRaisesRegex(RuntimeError, "different escalation"):
            await self.escalate_route(
                str(parent["decision_id"]), "escalation-session", "different evidence"
            )

    async def test_escalation_requires_enforced_marked_parent_and_same_identity(self) -> None:
        shadow = await self.call(self.durable_planning_route("shadow-session"))
        await self.call({
            "action": "route_feedback", "decision_id": shadow["decision_id"],
            "session_id": "shadow-session", "outcome": "escalated",
        })
        with self.assertRaisesRegex(RuntimeError, "must be enforced"):
            await self.escalate_route(str(shadow["decision_id"]), "shadow-session")

        self.supervisor.routing_mode = "surface_canary"
        parent = await self.call(self.durable_planning_route("owned-session"))
        with self.assertRaisesRegex(RuntimeError, "record escalated feedback"):
            await self.escalate_route(str(parent["decision_id"]), "owned-session")
        await self.call({
            "action": "route_feedback", "decision_id": parent["decision_id"],
            "session_id": "owned-session", "outcome": "escalated",
        })
        with self.assertRaisesRegex(RuntimeError, "does not belong"):
            await self.escalate_route(str(parent["decision_id"]), "other-session")

    async def test_escalation_stops_after_one_hop_and_respects_cooldown(self) -> None:
        self.supervisor.routing_mode = "surface_canary"
        parent = await self.call(self.durable_planning_route("bounded-session"))
        await self.call({
            "action": "route_feedback", "decision_id": parent["decision_id"],
            "session_id": "bounded-session", "outcome": "escalated",
        })
        self.supervisor.quota_routing_enabled = True
        self.supervisor.store.record_provider_rate_limit("kimi", time.time() + 60, "test")
        child = await self.escalate_route(str(parent["decision_id"]), "bounded-session")
        self.assertEqual("direct", child["lane"])

        await self.call({
            "action": "route_feedback", "decision_id": child["decision_id"],
            "session_id": "bounded-session", "outcome": "escalated",
        })
        with self.assertRaisesRegex(RuntimeError, "limited to one hop"):
            await self.escalate_route(str(child["decision_id"]), "bounded-session")

    async def test_v2_quota_rebalance_preserves_exact_provider_models(self) -> None:
        self.supervisor.quota_routing_enabled = True
        self.supervisor.routing_mode = "surface_canary"
        self.supervisor.store.record_provider_rate_limit("claude", time.time() + 60, "test")

        decision = await self.call({
            "action": "route_decide", "protocol_version": 2,
            "caller_provider": "codex", "surface": "codex",
            "capability": "planning",
            "surface_capabilities": {"durable_agent_jobs": True},
        })

        self.assertEqual("kimi", decision["provider"])
        self.assertEqual("kimi-code/k3", decision["model_alias"])
        self.assertEqual("claude", decision["fallback_provider"])
        self.assertEqual("opus", decision["fallback_model_alias"])

    async def test_quota_broker_rebalances_default_but_not_explicit_route(self) -> None:
        now = time.time()
        quota_dir = Path(os.environ["AGENT_JOB_QUOTA_HISTORY_DIR"])
        quota_dir.mkdir(parents=True)
        iso = lambda value: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))
        (quota_dir / "claude.json").write_text(json.dumps({
            "preferredAccountKey": "test",
            "accounts": {"test": [{
                "name": "session", "windowMinutes": 300,
                "entries": [{
                    "capturedAt": iso(now), "resetsAt": iso(now + 9000),
                    "usedPercent": 50,
                }],
            }]},
        }), encoding="utf-8")
        self.supervisor.quota_routing_enabled = True
        payload = {
            "action": "route_decide", "protocol_version": 1,
            "caller_provider": "codex", "surface": "codex",
            "capability": "planning",
        }

        balanced = await self.call(payload)
        explicit = await self.call({**payload, "explicit_provider": "claude"})

        self.assertEqual("kimi", balanced["provider"])
        self.assertEqual("claude", balanced["fallback_provider"])
        self.assertEqual("claude", explicit["provider"])

    async def test_exhausted_provider_preserves_explicit_route_and_submit(self) -> None:
        now = time.time()
        quota_dir = Path(os.environ["AGENT_JOB_QUOTA_HISTORY_DIR"])
        quota_dir.mkdir(parents=True)
        iso = lambda value: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))
        (quota_dir / "claude.json").write_text(json.dumps({
            "preferredAccountKey": "test",
            "accounts": {"test": [{
                "name": "weekly", "windowMinutes": 10080,
                "entries": [{
                    "capturedAt": iso(now), "resetsAt": iso(now + 9000),
                    "usedPercent": 99,
                }],
            }]},
        }), encoding="utf-8")
        self.supervisor.quota_routing_enabled = True
        self.supervisor.routing_mode = "surface_canary"

        decision = await self.call({
            "action": "route_decide", "protocol_version": 2,
            "caller_provider": "codex", "surface": "codex",
            "capability": "planning", "explicit_provider": "claude",
            "explicit_model": "opus", "session_id": "exhausted-explicit",
            "surface_capabilities": {"durable_agent_jobs": True},
        })
        self.assertEqual("agent_jobs", decision["lane"])
        self.assertEqual("claude", decision["provider"])
        submitted = await self.call(self.spec("complete"))
        result = await self.wait_for(str(submitted["job_id"]), {"completed"})
        self.assertEqual("completed", result["job"]["status"])

    def codex_native_route(self, session_id: str) -> dict[str, object]:
        return {
            "action": "route_decide", "protocol_version": 1,
            "caller_provider": "codex", "surface": "codex",
            "capability": "implementation", "complexity": "focused",
            "risk": "low", "scope": "single_module", "duration": "short",
            "durability": "session", "parallelizable": True,
            "surface_capabilities": {"native_subagents": True},
            "session_id": session_id,
        }

    async def test_codex_canary_atomically_reserves_native_capacity(self) -> None:
        self.supervisor.routing_mode = "codex_canary"
        self.supervisor.native_reservation_limit = 1

        first, second = await asyncio.gather(
            self.call(self.codex_native_route("session-a")),
            self.call(self.codex_native_route("session-b")),
        )

        lanes = sorted((str(first["lane"]), str(second["lane"])))
        self.assertEqual(["direct", "native_subagent"], lanes)
        admitted = first if first["lane"] == "native_subagent" else second
        self.assertEqual("active", admitted["reservation_status"])
        self.assertGreater(float(admitted["expires_at"]), time.time())

    async def test_native_reservation_capacity_is_machine_wide(self) -> None:
        self.supervisor.routing_mode = "codex_canary"
        self.supervisor.native_reservation_limit = 1
        first = await self.call(self.codex_native_route("session-a"))
        self.supervisor.store.db.execute(
            "UPDATE route_decisions SET surface = 'future-surface' WHERE decision_id = ?",
            (first["decision_id"],),
        )
        self.supervisor.store.db.commit()

        second = await self.call(self.codex_native_route("session-b"))

        self.assertEqual("direct", second["lane"])
        self.assertEqual("none", second["reservation_status"])

    async def test_route_feedback_is_idempotent_and_releases_reservation(self) -> None:
        self.supervisor.routing_mode = "codex_canary"
        decision = await self.call(self.codex_native_route("session-feedback"))
        payload = {
            "action": "route_feedback", "decision_id": decision["decision_id"],
            "session_id": "session-feedback", "outcome": "completed",
        }

        first = await self.call(payload)
        second = await self.call(payload)

        self.assertEqual("released", first["reservation_status"])
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        with self.assertRaisesRegex(RuntimeError, "conflicts"):
            await self.call({**payload, "outcome": "failed"})

    async def test_route_feedback_rejects_another_session(self) -> None:
        self.supervisor.routing_mode = "codex_canary"
        decision = await self.call(self.codex_native_route("session-owner"))

        with self.assertRaisesRegex(RuntimeError, "does not belong"):
            await self.call({
                "action": "route_feedback", "decision_id": decision["decision_id"],
                "session_id": "session-other", "outcome": "completed",
            })

    async def test_route_reconcile_releases_absent_session_reservations(self) -> None:
        self.supervisor.routing_mode = "codex_canary"
        first = await self.call(self.codex_native_route("session-reconcile"))
        second = await self.call(self.codex_native_route("session-reconcile"))

        result = await self.call({
            "action": "route_reconcile", "session_id": "session-reconcile",
            "active_decision_ids": [first["decision_id"]],
        })

        self.assertEqual([first["decision_id"]], result["retained_decision_ids"])
        self.assertEqual([second["decision_id"]], result["released_decision_ids"])

    async def test_route_status_expires_leases_and_reports_feedback_join_rate(self) -> None:
        self.supervisor.routing_mode = "codex_canary"
        completed = await self.call(self.codex_native_route("session-complete"))
        expired = await self.call(self.codex_native_route("session-expire"))
        await self.call({
            "action": "route_feedback", "decision_id": completed["decision_id"],
            "session_id": "session-complete", "outcome": "completed",
        })
        self.supervisor.store.db.execute(
            "UPDATE route_decisions SET expires_at = 1 WHERE decision_id = ?",
            (expired["decision_id"],),
        )
        self.supervisor.store.db.commit()

        status = await self.call({"action": "route_status"})

        self.assertEqual("codex_canary", status["routing_mode"])
        self.assertEqual(3, status["native_reservation_limit"])
        self.assertEqual(900, status["native_reservation_ttl_seconds"])
        self.assertEqual(1, status["native_reservations"]["released"])
        self.assertEqual(1, status["native_reservations"]["expired"])
        self.assertEqual(2, status["feedback_eligible"])
        self.assertEqual(1, status["feedback_joined"])
        self.assertEqual(0.5, status["feedback_join_rate"])
        self.assertEqual([1, 2], status["supported_protocol_versions"])
        self.assertEqual(2, status["latest_protocol_version"])
        self.assertEqual("opus", status["provider_capabilities"]["claude"]["deep_model"])
        self.assertIn("durable_agent_jobs", status["surface_capabilities"]["kimi-code"])

    async def test_route_decide_rejects_unknown_protocol_without_persisting(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "protocol version"):
            await self.call({
                "action": "route_decide", "protocol_version": 99,
                "caller_provider": "codex", "surface": "codex",
                "capability": "planning",
            })
        count = self.supervisor.store.db.execute(
            "SELECT COUNT(*) FROM route_decisions"
        ).fetchone()[0]
        self.assertEqual(0, count)

    async def test_route_decide_canonicalizes_and_discards_unknown_persisted_fields(self) -> None:
        decision = await self.call({
            "action": "route_decide", "protocol_version": 1,
            "caller_provider": " Codex ", "surface": " CODEX ",
            "capability": " Planning ", "future_field": "do not persist",
        })
        row = self.supervisor.store.db.execute(
            "SELECT * FROM route_decisions WHERE decision_id = ?",
            (decision["decision_id"],),
        ).fetchone()
        self.assertEqual("codex", row["caller_provider"])
        self.assertEqual("codex", row["surface"])
        self.assertEqual("planning", row["capability"])
        self.assertNotIn("future_field", json.loads(row["request_json"]))

    async def test_route_decide_rejects_oversized_intent(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "exceeds"):
            await self.call({
                "action": "route_decide", "protocol_version": 1,
                "caller_provider": "codex", "surface": "codex",
                "capability": "planning", "future_field": "x" * 20_000,
            })

    async def test_prune_removes_expired_route_decisions(self) -> None:
        decision = await self.call({
            "action": "route_decide", "protocol_version": 1,
            "caller_provider": "codex", "surface": "codex",
            "capability": "planning",
        })
        self.supervisor.store.db.execute(
            "UPDATE route_decisions SET created_at = 1 WHERE decision_id = ?",
            (decision["decision_id"],),
        )
        self.supervisor.store.db.commit()
        self.supervisor.store.prune(2)
        row = self.supervisor.store.db.execute(
            "SELECT 1 FROM route_decisions WHERE decision_id = ?",
            (decision["decision_id"],),
        ).fetchone()
        self.assertIsNone(row)

    async def test_prune_preserves_active_route_reservations(self) -> None:
        self.supervisor.routing_mode = "codex_canary"
        decision = await self.call(self.codex_native_route("session-active-prune"))
        self.supervisor.store.db.execute(
            "UPDATE route_decisions SET created_at = 1 WHERE decision_id = ?",
            (decision["decision_id"],),
        )
        self.supervisor.store.db.commit()

        self.supervisor.store.prune(2)

        row = self.supervisor.store.db.execute(
            "SELECT reservation_status FROM route_decisions WHERE decision_id = ?",
            (decision["decision_id"],),
        ).fetchone()
        self.assertEqual("active", row["reservation_status"])

    async def test_cao_backend_fails_closed_for_unenforceable_contracts(self) -> None:
        with patch.dict(os.environ, {"AGENT_JOB_EXECUTION_BACKEND": "cao"}):
            codex = self.spec("review")
            codex.update(provider="codex", model="gpt-5.5-codex")
            with self.assertRaisesRegex(ValueError, "read-only Codex"):
                self.supervisor.submit(codex)

            claude = self.spec("review")
            claude.update(provider="claude", model="opus", max_turns=2)
            with self.assertRaisesRegex(ValueError, "turn ceiling"):
                self.supervisor.submit(claude)

    async def test_submit_persists_execution_backend(self) -> None:
        with patch.dict(os.environ, {"AGENT_JOB_EXECUTION_BACKEND": "cao"}):
            spec = self.spec("review")
            spec.update(provider="claude", model="opus", max_turns=0)
            job = self.supervisor.submit(spec)

        self.assertEqual("cao", job["execution_backend"])

    async def test_cao_canary_requires_provider_and_owner_prefix(self) -> None:
        canary = self.spec("complete")
        canary["owner"] = "cao-canary:phase-7"
        canary["max_turns"] = 0
        ordinary = self.spec("complete")
        ordinary["owner"] = "codex:ordinary"
        with patch.dict(
            os.environ,
            {
                "AGENT_JOB_EXECUTION_BACKEND": "native",
                "AGENT_JOB_CAO_CANARY_PROVIDERS": "claude",
                "AGENT_JOB_CAO_CANARY_OWNER_PREFIXES": "cao-canary:",
            },
            clear=False,
        ):
            selected = self.supervisor.submit(canary)
            native = self.supervisor.submit(ordinary)

        self.assertEqual("cao", selected["execution_backend"])
        self.assertEqual("native", native["execution_backend"])
        await self.call({"action": "cancel", "job_id": selected["job_id"]})
        await self.call({"action": "cancel", "job_id": native["job_id"]})

    async def test_cao_provider_promotion_is_owner_independent(self) -> None:
        spec = self.spec("complete")
        spec.update(owner="ordinary-owner", max_turns=0)
        with patch.dict(
            os.environ,
            {"AGENT_JOB_EXECUTION_BACKEND": "native", "AGENT_JOB_CAO_PROVIDERS": "claude"},
            clear=False,
        ):
            job = self.supervisor.submit(spec)
        self.assertEqual("cao", job["execution_backend"])
        await self.call({"action": "cancel", "job_id": job["job_id"]})

    async def test_invalid_default_execution_backend_fails_closed(self) -> None:
        with patch.dict(os.environ, {"AGENT_JOB_EXECUTION_BACKEND": "invalid"}):
            with self.assertRaisesRegex(ValueError, "Unsupported execution backend"):
                self.supervisor.submit(self.spec("complete"))


if __name__ == "__main__":
    unittest.main()
