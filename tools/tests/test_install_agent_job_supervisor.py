from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

import install_agent_job_supervisor as installer  # noqa: E402


class SupervisorInstallerTest(unittest.TestCase):
    def test_service_environment_forwards_cao_canary_configuration(self) -> None:
        values = {
            "AGENT_JOB_EXECUTION_BACKEND": "native",
            "AGENT_JOB_CAO_URL": "http://127.0.0.1:9889",
            "AGENT_JOB_CAO_TOKEN": "token",
            "AGENT_JOB_CAO_LAUNCH_TIMEOUT": "7",
            "AGENT_JOB_CAO_PROVIDERS": "kimi",
            "AGENT_JOB_CAO_CANARY_PROVIDERS": "claude",
            "AGENT_JOB_CAO_CANARY_OWNER_PREFIXES": "cao-canary:abc:",
            "AGENT_JOB_CLAUDE_CONCURRENCY": "2",
            "AGENT_JOB_CODEX_CONCURRENCY": "3",
            "AGENT_JOB_KIMI_CONCURRENCY": "1",
            "AGENT_JOB_KIMI_DEFAULT_MODEL": "kimi-code/k3",
            "AGENT_JOB_MAX_LOG_BYTES": "1000",
            "AGENT_JOB_MAX_EVENT_BYTES": "2000",
            "AGENT_JOB_MAX_PARTIAL_RESPONSE_BYTES": "3000",
            "AGENT_JOB_RETENTION_SECONDS": "4000",
            "AGENT_JOB_ROUTING_MODE": "codex_canary",
            "AGENT_JOB_QUOTA_ROUTING": "1",
            "AGENT_JOB_QUOTA_HISTORY_DIR": "/tmp/quota-history",
            "AGENT_JOB_QUOTA_STALE_SECONDS": "7200",
            "AGENT_JOB_RATE_LIMIT_COOLDOWN_SECONDS": "900",
            "AGENT_JOB_CODEX_NATIVE_RESERVATIONS": "3",
            "AGENT_JOB_ROUTE_RESERVATION_SECONDS": "900",
        }
        with patch.dict(os.environ, values, clear=False):
            environment = installer._service_environment()

        for name, value in values.items():
            self.assertEqual(value, environment[name])

    def test_active_jobs_collects_launching_and_running(self) -> None:
        responses = [
            {"jobs": [{"job_id": "launch"}]},
            {"jobs": [{"job_id": "run"}]},
        ]
        with patch.object(installer, "_socket_request", side_effect=responses):
            self.assertEqual(["launch", "run"], [job["job_id"] for job in installer._active_jobs()])

    def test_install_refuses_to_interrupt_active_jobs_before_writing(self) -> None:
        with patch.object(installer, "_socket_request", return_value={"pid": 123}), \
             patch.object(installer, "_active_jobs", return_value=[{"job_id": "active"}]), \
             patch.object(installer, "_run") as run:
            with self.assertRaisesRegex(RuntimeError, "Refusing to replace"):
                installer.install()
        run.assert_not_called()

    def test_active_job_check_fails_closed_on_unresponsive_supervisor(self) -> None:
        with patch.object(installer, "_socket_request", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Cannot verify active jobs"):
                installer._active_jobs()


if __name__ == "__main__":
    unittest.main()
