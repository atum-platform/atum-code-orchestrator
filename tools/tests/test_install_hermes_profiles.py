from __future__ import annotations

from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch


TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

import install_hermes_profiles as migration  # noqa: E402
import agent_job_policy  # noqa: E402


OLD_CONFIG = """mcp_servers:
  review-sidecars:
    command: old-python
    args:
    - old-server.py
    enabled: true
skills:
- review-sidecars
- another-skill
"""


class ProfileMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profiles = self.root / "profiles"
        self.profile = self.profiles / "atum-test"
        self.profile.mkdir(parents=True)
        self.config = self.profile / "config.yaml"
        self.config.write_text(OLD_CONFIG, encoding="utf-8")
        legacy = self.profile / "skills" / "review-sidecars"
        legacy.mkdir(parents=True)
        (legacy / "SKILL.md").write_text("legacy", encoding="utf-8")
        self.source = self.root / "agent-jobs"
        self.source.mkdir()
        (self.source / "SKILL.md").write_text("new", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_dry_run_does_not_mutate(self) -> None:
        changed = migration.migrate_profiles(self.profiles, self.source, "test", False)
        self.assertEqual(["atum-test"], changed)
        self.assertEqual(OLD_CONFIG, self.config.read_text(encoding="utf-8"))
        self.assertFalse((self.profile / "skills" / "agent-jobs").exists())

    def test_apply_is_transactional_and_then_idempotent(self) -> None:
        changed = migration.migrate_profiles(self.profiles, self.source, "test", True)
        self.assertEqual(["atum-test"], changed)
        result = self.config.read_text(encoding="utf-8")
        self.assertIn("  agent-jobs:\n", result)
        self.assertIn("- agent-jobs\n", result)
        self.assertNotIn("  review-sidecars:\n", result)
        self.assertTrue(self.config.with_name("config.yaml.bak.agent-jobs-test").exists())
        self.assertEqual("new", (self.profile / "skills" / "agent-jobs" / "SKILL.md").read_text())
        self.assertTrue((self.profile / "agent-jobs-backups" / "review-sidecars-test").is_dir())
        self.assertEqual([], migration.migrate_profiles(self.profiles, self.source, "second", True))

    def test_malformed_profile_aborts_instead_of_silently_skipping(self) -> None:
        duplicate = OLD_CONFIG + "  review-sidecars:\n    command: duplicate\n"
        self.config.write_text(duplicate, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "expected one"):
            migration.migrate_profiles(self.profiles, self.source, "bad", False)

    def test_failed_config_write_restores_both_skill_directories(self) -> None:
        destination = self.profile / "skills" / "agent-jobs"
        destination.mkdir()
        (destination / "SKILL.md").write_text("previous", encoding="utf-8")
        real_write = migration._atomic_write
        calls = 0

        def fail_once(path: Path, text: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("synthetic write failure")
            real_write(path, text)

        with patch.object(migration, "_atomic_write", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "synthetic"):
                migration.migrate_profiles(self.profiles, self.source, "rollback", True)
        self.assertEqual(OLD_CONFIG, self.config.read_text(encoding="utf-8"))
        self.assertEqual("previous", (destination / "SKILL.md").read_text(encoding="utf-8"))
        self.assertEqual(
            "legacy",
            (self.profile / "skills" / "review-sidecars" / "SKILL.md").read_text(encoding="utf-8"),
        )
        self.assertFalse(self.config.with_name("config.yaml.bak.agent-jobs-rollback").exists())

    def test_blank_line_inside_old_block_does_not_leave_old_keys(self) -> None:
        config = OLD_CONFIG.replace("    enabled: true\n", "\n    enabled: true\n")
        result = migration.migrate_config(config, "atum-test")
        self.assertEqual(1, result.count("    enabled: true\n"))
        self.assertNotIn("old-server.py", result)

    def test_existing_profile_timing_settings_are_preserved(self) -> None:
        config = OLD_CONFIG.replace("    enabled: true\n", "    enabled: false\n    timeout: 600\n    connect_timeout: 45\n")
        result = migration.migrate_config(config, "atum-test")
        self.assertIn("    enabled: false\n", result)
        self.assertIn("    timeout: 600\n", result)
        self.assertIn("    connect_timeout: 45\n", result)

    def test_existing_env_without_allowed_roots_is_extended_once(self) -> None:
        config = OLD_CONFIG.replace("    enabled: true\n", "    enabled: true\n    env:\n      KEEP: yes\n")
        result = migration.migrate_config(config, "atum-test")
        self.assertEqual(1, result.count("    env:\n"))
        self.assertIn("      KEEP: yes\n", result)
        self.assertEqual(1, result.count("      AGENT_JOB_ALLOWED_ROOTS:"))

    def test_later_profile_failure_rolls_back_earlier_profiles(self) -> None:
        second = self.profiles / "atum-two"
        second.mkdir()
        second.joinpath("config.yaml").write_text(OLD_CONFIG, encoding="utf-8")
        real_migrate = migration._migrate_profile
        calls = 0
        def fail_second(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("second profile failed")
            return real_migrate(*args)
        with patch.object(migration, "_migrate_profile", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "second profile failed"):
                migration.migrate_profiles(self.profiles, self.source, "global-rollback", True)
        self.assertEqual(OLD_CONFIG, self.config.read_text(encoding="utf-8"))
        self.assertTrue((self.profile / "skills/review-sidecars/SKILL.md").is_file())
        self.assertFalse((self.profile / "skills/agent-jobs").exists())

    def test_skill_comparison_hashes_content_not_only_metadata(self) -> None:
        destination = self.profile / "skills" / "agent-jobs"
        destination.mkdir()
        target = destination / "SKILL.md"
        target.write_text("old", encoding="utf-8")
        self.source.joinpath("SKILL.md").write_text("new", encoding="utf-8")
        timestamp = 1_700_000_000
        os.utime(target, (timestamp, timestamp))
        os.utime(self.source / "SKILL.md", (timestamp, timestamp))
        self.assertFalse(migration._same_skill(self.source, destination))

    def test_profile_root_policy_comes_from_shared_policy(self) -> None:
        self.assertIn(agent_job_policy.allowed_roots_value(), "".join(migration._server_block()))

    def test_generated_bytecode_does_not_create_skill_drift(self) -> None:
        destination = self.profile / "skills" / "agent-jobs"
        shutil_source = self.source / "scripts"
        shutil_source.mkdir()
        shutil_source.joinpath("review.py").write_text("pass\n", encoding="utf-8")
        destination.mkdir()
        (destination / "SKILL.md").write_text("new", encoding="utf-8")
        destination.joinpath("scripts").mkdir()
        destination.joinpath("scripts/review.py").write_text("pass\n", encoding="utf-8")
        cache = destination / "scripts" / "__pycache__"
        cache.mkdir()
        cache.joinpath("review.cpython-311.pyc").write_bytes(b"generated")
        self.assertTrue(migration._same_skill(self.source, destination))


if __name__ == "__main__":
    unittest.main()
