from __future__ import annotations

import os
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import patch


TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

import install_agent_job_supervisor as installer  # noqa: E402


class SupervisorInstallerTest(unittest.TestCase):
    def setUp(self) -> None:
        # Never read the host's real LaunchAgent: its retained values would leak
        # machine state into every _service_environment() call.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        plist = patch.object(installer, "PLIST_PATH", Path(temp.name) / "absent.plist")
        plist.start()
        self.addCleanup(plist.stop)

    def _environment_with_existing(
        self, existing: dict[str, str], overrides: dict[str, str],
    ) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            plist_path = Path(temp_dir) / "supervisor.plist"
            with plist_path.open("wb") as handle:
                plistlib.dump({"EnvironmentVariables": existing}, handle)
            with patch.object(installer, "PLIST_PATH", plist_path), \
                 patch.dict(os.environ, overrides, clear=True):
                return installer._service_environment()

    def test_service_environment_retains_known_existing_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plist_path = Path(temp_dir) / "supervisor.plist"
            with plist_path.open("wb") as handle:
                plistlib.dump(
                    {
                        "EnvironmentVariables": {
                            "AGENT_JOB_ROUTING_MODE": "surface_canary",
                            "AGENT_JOB_QUOTA_ROUTING": "1",
                            "AGENT_JOB_DYNAMIC_CONCURRENCY": "1",
                            "AGENT_JOB_OPENCODE_DEFAULT_MODEL": "opencode-go/kimi-k3",
                            "AGENT_JOB_KIMI_BIN": "/stale/kimi",
                            "AGENT_JOB_ALLOWED_ROOTS": "/stale/policy/root",
                            "UNRELATED_VALUE": "discard-me",
                        }
                    },
                    handle,
                )
            with patch.object(installer, "PLIST_PATH", plist_path), \
                 patch.dict(os.environ, {}, clear=True):
                environment = installer._service_environment()

        self.assertEqual("surface_canary", environment["AGENT_JOB_ROUTING_MODE"])
        self.assertEqual("1", environment["AGENT_JOB_QUOTA_ROUTING"])
        self.assertEqual("1", environment["AGENT_JOB_DYNAMIC_CONCURRENCY"])
        self.assertEqual("opencode-go/kimi-k3", environment["AGENT_JOB_OPENCODE_DEFAULT_MODEL"])
        # Kimi settings from an older install are dropped on reinstall.
        self.assertNotIn("AGENT_JOB_KIMI_BIN", environment)
        self.assertNotEqual("/stale/policy/root", environment["AGENT_JOB_ALLOWED_ROOTS"])
        self.assertNotIn("UNRELATED_VALUE", environment)

    def test_service_environment_explicit_override_wins_over_existing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plist_path = Path(temp_dir) / "supervisor.plist"
            with plist_path.open("wb") as handle:
                plistlib.dump(
                    {"EnvironmentVariables": {"AGENT_JOB_ROUTING_MODE": "shadow"}},
                    handle,
                )
            with patch.object(installer, "PLIST_PATH", plist_path), patch.dict(
                os.environ, {"AGENT_JOB_ROUTING_MODE": "surface_canary"}, clear=True
            ):
                environment = installer._service_environment()

        self.assertEqual("surface_canary", environment["AGENT_JOB_ROUTING_MODE"])

    def test_launchd_transition_budget_allows_thirty_seconds(self) -> None:
        self.assertEqual(300, installer.SERVICE_TRANSITION_POLLS)

    def test_service_environment_forwards_cao_canary_configuration(self) -> None:
        values = {
            "AGENT_JOB_EXECUTION_BACKEND": "native",
            "AGENT_JOB_CAO_URL": "http://127.0.0.1:9889",
            "AGENT_JOB_CAO_TOKEN": "token",
            "AGENT_JOB_CAO_LAUNCH_TIMEOUT": "7",
            "AGENT_JOB_CAO_PROVIDERS": "claude",
            "AGENT_JOB_CAO_CANARY_PROVIDERS": "claude",
            "AGENT_JOB_CAO_CANARY_OWNER_PREFIXES": "cao-canary:abc:",
            "AGENT_JOB_CLAUDE_CONCURRENCY": "2",
            "AGENT_JOB_CODEX_CONCURRENCY": "3",
            "AGENT_JOB_MAX_LOG_BYTES": "1000",
            "AGENT_JOB_MAX_EVENT_BYTES": "2000",
            "AGENT_JOB_MAX_PARTIAL_RESPONSE_BYTES": "3000",
            "AGENT_JOB_RETENTION_SECONDS": "4000",
            "AGENT_JOB_ROUTING_MODE": "codex_canary",
            "AGENT_JOB_QUOTA_ROUTING": "1",
            "AGENT_JOB_QUOTA_HISTORY_DIR": "/tmp/quota-history",
            "AGENT_JOB_QUOTA_STALE_SECONDS": "7200",
            "AGENT_JOB_RATE_LIMIT_COOLDOWN_SECONDS": "900",
            "AGENT_JOB_DYNAMIC_CONCURRENCY": "1",
            "AGENT_JOB_NATIVE_RESERVATIONS": "5",
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

    def test_install_rejects_dynamic_concurrency_without_quota_before_service_swap(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENT_JOB_DYNAMIC_CONCURRENCY": "true", "AGENT_JOB_QUOTA_ROUTING": "off"},
        ), patch.object(installer, "_socket_request") as socket_request, \
             patch.object(installer, "_run") as run:
            with self.assertRaisesRegex(RuntimeError, "requires AGENT_JOB_QUOTA_ROUTING"):
                installer.install()
        socket_request.assert_not_called()
        run.assert_not_called()

    def test_install_rejects_invalid_boolean_before_service_swap(self) -> None:
        with patch.dict(os.environ, {"AGENT_JOB_DYNAMIC_CONCURRENCY": "sometimes"}), \
             patch.object(installer, "_socket_request") as socket_request, \
             patch.object(installer, "_run") as run:
            with self.assertRaisesRegex(RuntimeError, "must be a boolean"):
                installer.install()
        socket_request.assert_not_called()
        run.assert_not_called()

    def _installed_plist(self, temp_dir: str) -> dict[str, object]:
        root = Path(temp_dir)
        plist_path = root / "service.plist"
        with patch.object(installer, "PLIST_PATH", plist_path), \
             patch.object(installer, "STATE_DIR", root / "state"), \
             patch.object(installer, "IMPLEMENT_TOKEN_PATH", root / "state" / "implement.token"), \
             patch.object(installer, "SERVICE_TRANSITION_POLLS", 1), \
             patch.object(installer, "_socket_request", return_value=None), \
             patch.object(installer, "_run"):
            with self.assertRaises(RuntimeError):
                # Readiness polling needs a live socket; the plist is already
                # written by the time that check fails.
                installer.install()
        with plist_path.open("rb") as handle:
            return plistlib.load(handle)

    def test_service_restarts_after_a_clean_signal_shutdown(self) -> None:
        # A SIGTERM'd supervisor exits 0. {"SuccessfulExit": False} told launchd
        # to leave it down, which is what turned the 2026-09-04 signal into an
        # outage lasting until a human restarted the service.
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = self._installed_plist(temp_dir)
        self.assertIs(True, payload["KeepAlive"])

    def test_service_exit_timeout_exceeds_supervisor_shutdown_budget(self) -> None:
        sys.path.insert(0, str(TOOLS_DIR))
        import agent_job_supervisor  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as temp_dir:
            payload = self._installed_plist(temp_dir)
        self.assertGreater(
            int(payload["ExitTimeOut"]), agent_job_supervisor.SHUTDOWN_GRACE_SECONDS
        )

    def test_active_job_check_fails_closed_on_unresponsive_supervisor(self) -> None:
        with patch.object(installer, "_socket_request", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Cannot verify active jobs"):
                installer._active_jobs()

    def test_claude_binary_is_left_to_per_launch_discovery(self) -> None:
        self.assertNotIn("AGENT_JOB_CLAUDE_BIN", self._environment_with_existing({}, {}))

    def test_retained_claude_binary_pin_is_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pinned = Path(temp_dir) / "claude-code" / "2.1.280" / "claude"
            pinned.parent.mkdir(parents=True)
            pinned.write_bytes(b"\xcf\xfa\xed\xfe")
            environment = self._environment_with_existing(
                {"AGENT_JOB_CLAUDE_BIN": str(pinned)}, {},
            )
        self.assertNotIn("AGENT_JOB_CLAUDE_BIN", environment)

    def test_explicit_claude_binary_is_persisted_without_resolving_links(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "claude-2.1.281"
            target.write_bytes(b"\xcf\xfa\xed\xfe")
            launcher = Path(temp_dir) / "claude"
            launcher.symlink_to(target)
            environment = self._environment_with_existing(
                {"AGENT_JOB_CLAUDE_BIN": "/retained/claude"},
                {"AGENT_JOB_CLAUDE_BIN": str(launcher)},
            )
        self.assertEqual(str(launcher), environment["AGENT_JOB_CLAUDE_BIN"])

    def test_retained_claude_script_requires_a_credential_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wrapper = Path(temp_dir) / "claude-review-runtime"
            wrapper.write_text('#!/usr/bin/env bash\nexec claude "$@"\n')
            missing = Path(temp_dir) / "missing.env"
            for overrides in ({}, {"AGENT_JOB_PROFILE_ENV": str(missing)}):
                with self.subTest(overrides=overrides), \
                     self.assertRaisesRegex(RuntimeError, "AGENT_JOB_PROFILE_ENV"):
                    self._environment_with_existing(
                        {"AGENT_JOB_CLAUDE_BIN": str(wrapper)}, overrides,
                    )

    def test_retained_claude_script_yields_to_profile_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wrapper = Path(temp_dir) / "claude-review-runtime"
            wrapper.write_text('#!/usr/bin/env bash\nexec claude "$@"\n')
            profile = Path(temp_dir) / "profile.env"
            profile.write_text("CLAUDE_CODE_OAUTH_TOKEN=placeholder\n")
            environment = self._environment_with_existing(
                {"AGENT_JOB_CLAUDE_BIN": str(wrapper)},
                {"AGENT_JOB_PROFILE_ENV": str(profile)},
            )
        self.assertNotIn("AGENT_JOB_CLAUDE_BIN", environment)
        self.assertEqual(str(profile), environment["AGENT_JOB_PROFILE_ENV"])

    def test_explicit_claude_binary_keeps_a_script_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wrapper = Path(temp_dir) / "claude"
            wrapper.write_text('#!/usr/bin/env bash\nexec claude "$@"\n')
            environment = self._environment_with_existing(
                {"AGENT_JOB_CLAUDE_BIN": str(wrapper)},
                {"AGENT_JOB_CLAUDE_BIN": str(wrapper)},
            )
        self.assertEqual(str(wrapper), environment["AGENT_JOB_CLAUDE_BIN"])

    def test_install_refuses_to_drop_a_script_launcher_before_service_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wrapper = Path(temp_dir) / "claude"
            wrapper.write_text("#!/usr/bin/env bash\n")
            plist_path = Path(temp_dir) / "supervisor.plist"
            with plist_path.open("wb") as handle:
                plistlib.dump({"EnvironmentVariables": {"AGENT_JOB_CLAUDE_BIN": str(wrapper)}}, handle)
            with patch.object(installer, "PLIST_PATH", plist_path), \
                 patch.dict(os.environ, {}, clear=True), \
                 patch.object(installer, "_socket_request") as socket_request, \
                 patch.object(installer, "_run") as run:
                with self.assertRaisesRegex(RuntimeError, "launches Claude through the script"):
                    installer.install()
        socket_request.assert_not_called()
        run.assert_not_called()


    def test_opencode_settings_persist_but_its_binary_is_never_discovered(self) -> None:
        retained = {
            "AGENT_JOB_OPENCODE_DEFAULT_MODEL": "opencode-go/kimi-k3",
            "AGENT_JOB_OPENCODE_MODEL_PREFIXES": "opencode-go/",
            "AGENT_JOB_OPENCODE_CONCURRENCY": "2",
        }
        environment = self._environment_with_existing(retained, {})
        for name, value in retained.items():
            self.assertEqual(value, environment[name])
        # Homebrew's stable link points into a versioned Cellar path that
        # upgrades remove, so only a deliberate pin is persisted.
        self.assertNotIn("AGENT_JOB_OPENCODE_BIN", environment)
        pinned = self._environment_with_existing({}, {"AGENT_JOB_OPENCODE_BIN": "/opt/homebrew/bin/opencode"})
        self.assertEqual("/opt/homebrew/bin/opencode", pinned["AGENT_JOB_OPENCODE_BIN"])

    def test_profile_env_path_list_must_be_complete_to_drop_a_script_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wrapper = Path(temp_dir) / "claude"
            wrapper.write_text("#!/usr/bin/env bash\n")
            claude_env = Path(temp_dir) / "claude.env"
            claude_env.write_text("CLAUDE_CODE_OAUTH_TOKEN=placeholder\n")
            opencode_env = Path(temp_dir) / "opencode.env"
            existing = {"AGENT_JOB_CLAUDE_BIN": str(wrapper)}
            incomplete = os.pathsep.join((str(claude_env), str(opencode_env)))
            with self.assertRaisesRegex(RuntimeError, "AGENT_JOB_PROFILE_ENV"):
                self._environment_with_existing(existing, {"AGENT_JOB_PROFILE_ENV": incomplete})
            opencode_env.write_text("OPENCODE_API_KEY=placeholder\n")
            environment = self._environment_with_existing(
                existing, {"AGENT_JOB_PROFILE_ENV": incomplete},
            )
        self.assertEqual(incomplete, environment["AGENT_JOB_PROFILE_ENV"])
        self.assertNotIn("AGENT_JOB_CLAUDE_BIN", environment)

if __name__ == "__main__":
    unittest.main()
