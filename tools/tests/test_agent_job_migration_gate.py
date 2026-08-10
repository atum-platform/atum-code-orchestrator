from __future__ import annotations

from datetime import datetime, timezone
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import plistlib
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

from agent_job_migration_gate import acceptance_checks, main, observation_check  # noqa: E402

TEST_COMMIT = "a" * 40


class MigrationGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = time.time()
        self.database_index = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def report(
        self, provider: str, commit: str = TEST_COMMIT, overall: str = "passed"
    ) -> Path:
        path = self.root / f"{provider}.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": "atum.cao.acceptance/v1",
                    "provider": provider,
                    "model": "opus" if provider == "claude_code" else None,
                    "mode": "gate",
                    "overall": overall,
                    "source_commit": commit,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "checks": [{"name": "required", "required": True, "status": "pass"}],
                }
            ),
            encoding="utf-8",
        )
        return path

    def database(self, statuses: list[str], age_hours: float = 25) -> Path:
        self.database_index += 1
        path = self.root / f"jobs-{self.database_index}.sqlite3"
        db = sqlite3.connect(path)
        db.execute(
            """CREATE TABLE jobs (
                status TEXT, failure_kind TEXT, created_at REAL, finished_at REAL,
                execution_backend TEXT, provider TEXT, model TEXT, owner TEXT
            )"""
        )
        interval = age_hours * 3600 / max(1, len(statuses) - 1)
        for index, status in enumerate(statuses):
            finished_at = self.now - age_hours * 3600 + index * interval
            db.execute(
                "INSERT INTO jobs VALUES (?, '', ?, ?, 'cao', 'claude', 'opus', "
                f"'cao-canary:{TEST_COMMIT}:test')",
                (status, finished_at - 60, finished_at),
            )
        db.commit()
        db.close()
        return path

    def service_plist(self) -> Path:
        path = self.root / "agent-job-supervisor.plist"
        with path.open("wb") as handle:
            plistlib.dump(
                {
                    "EnvironmentVariables": {
                        "AGENT_JOB_EXECUTION_BACKEND": "native",
                        "AGENT_JOB_CAO_URL": "http://127.0.0.1:9889",
                        "AGENT_JOB_CAO_CANARY_PROVIDERS": "claude",
                        "AGENT_JOB_CAO_CANARY_OWNER_PREFIXES": (
                            f"cao-canary:{TEST_COMMIT}:"
                        ),
                    }
                },
                handle,
            )
        return path

    def test_acceptance_requires_mock_provider_commit_model_and_pass(self) -> None:
        checks = acceptance_checks(
            [self.report("mock_cli"), self.report("claude_code")],
            "claude",
            TEST_COMMIT,
            "opus",
            self.now,
        )
        self.assertEqual(["pass", "pass"], [check["status"] for check in checks])

        checks = acceptance_checks(
            [self.report("mock_cli"), self.report("claude_code", commit="wrong")],
            "claude",
            TEST_COMMIT,
            "opus",
            self.now,
        )
        self.assertEqual("fail", checks[0]["status"])

    def test_observation_passes_only_with_volume_time_and_reliability(self) -> None:
        passed = observation_check(
            self.database(["completed"] * 5),
            "claude",
            "opus",
            f"cao-canary:{TEST_COMMIT}:",
            5,
            24,
            0.1,
            self.now,
        )
        self.assertEqual("pass", passed["status"])

        failed = observation_check(
            self.database(["completed", "interrupted"]),
            "claude",
            "opus",
            f"cao-canary:{TEST_COMMIT}:",
            2,
            24,
            0.5,
            self.now,
        )
        self.assertEqual("fail", failed["status"])
        self.assertIn("interrupted", failed["detail"])

        cancelled = observation_check(
            self.database(["cancelled"] * 5),
            "claude",
            "opus",
            f"cao-canary:{TEST_COMMIT}:",
            5,
            24,
            0.1,
            self.now,
        )
        self.assertEqual("fail", cancelled["status"])
        self.assertIn("completed canary jobs", cancelled["detail"])

    def test_observation_filters_model_and_requires_completion_span(self) -> None:
        wrong_model = self.database(["completed"] * 5)
        filtered = observation_check(
            wrong_model,
            "claude",
            "sonnet",
            f"cao-canary:{TEST_COMMIT}:",
            5,
            24,
            0.1,
            self.now,
        )
        self.assertEqual(0, filtered["evidence"]["completed_jobs"])

        short_span = observation_check(
            self.database(["completed"] * 5, age_hours=1),
            "claude",
            "opus",
            f"cao-canary:{TEST_COMMIT}:",
            5,
            24,
            0.1,
            self.now,
        )
        self.assertEqual("fail", short_span["status"])
        self.assertIn("completion-span", short_span["detail"])

    def test_observation_missing_database_fails_without_creating_it(self) -> None:
        path = self.root / "missing.sqlite3"
        result = observation_check(
            path,
            "claude",
            "opus",
            f"cao-canary:{TEST_COMMIT}:",
            5,
            24,
            0.1,
            self.now,
        )
        self.assertEqual("fail", result["status"])
        self.assertFalse(path.exists())

    def test_observation_invalid_completed_timestamp_holds_cleanly(self) -> None:
        path = self.database(["completed"] * 5)
        db = sqlite3.connect(path)
        db.execute("UPDATE jobs SET finished_at = NULL WHERE rowid = 1")
        db.commit()
        db.close()

        result = observation_check(
            path,
            "claude",
            "opus",
            f"cao-canary:{TEST_COMMIT}:",
            5,
            24,
            0.1,
            self.now,
        )
        self.assertEqual("fail", result["status"])
        self.assertIn("invalid finished_at", result["detail"])

    def test_main_emits_auditable_parameters_and_canonical_namespace(self) -> None:
        database = self.database(["completed"] * 5)
        output = io.StringIO()
        argv = [
            "agent_job_migration_gate.py",
            "--provider",
            "claude",
            "--source-commit",
            TEST_COMMIT,
            "--model",
            "opus",
            "--acceptance-report",
            str(self.report("mock_cli")),
            "--acceptance-report",
            str(self.report("claude_code")),
            "--db",
            str(database),
            "--service-plist",
            str(self.service_plist()),
        ]
        with patch.object(sys, "argv", argv), redirect_stdout(output):
            self.assertEqual(0, main())

        result = json.loads(output.getvalue())
        self.assertEqual("promote", result["verdict"])
        self.assertEqual("opus", result["model"])
        self.assertEqual(f"cao-canary:{TEST_COMMIT}:", result["owner_prefix"])
        self.assertEqual(2, len(result["acceptance_reports"]))
        self.assertTrue(all(item["sha256"] for item in result["acceptance_reports"]))
        self.assertEqual(
            {"min_jobs": 5, "min_hours": 24.0, "max_failure_rate": 0.1},
            result["thresholds"],
        )


if __name__ == "__main__":
    unittest.main()
