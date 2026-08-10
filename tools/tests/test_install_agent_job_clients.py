from __future__ import annotations

import json
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch


TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

import install_agent_job_clients as installer  # noqa: E402


class ClientInstallerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_mcp_merge_preserves_unrelated_configuration(self) -> None:
        path = self.root / "config.json"
        path.write_text(
            json.dumps({"preferences": {"theme": "dark"}, "mcpServers": {"other": {"url": "x"}}}),
            encoding="utf-8",
        )
        self.assertTrue(installer.merge_mcp_config(path, "test", True))
        result = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("dark", result["preferences"]["theme"])
        self.assertEqual({"url": "x"}, result["mcpServers"]["other"])
        self.assertEqual(installer.server_config(), result["mcpServers"]["agent-jobs"])
        self.assertTrue(path.with_name("config.json.bak.agent-jobs-test").exists())
        self.assertFalse(installer.merge_mcp_config(path, "second", True))

    def test_dry_run_does_not_create_mcp_file(self) -> None:
        path = self.root / "mcp.json"
        self.assertTrue(installer.merge_mcp_config(path, "test", False))
        self.assertFalse(path.exists())

    def test_guidance_markers_replace_only_managed_section(self) -> None:
        path = self.root / "AGENTS.md"
        path.write_text("# Existing\n\nKeep this.\n", encoding="utf-8")
        self.assertTrue(installer.merge_kimi_guidance(path, "test", True))
        first = path.read_text(encoding="utf-8")
        self.assertIn("Keep this.", first)
        self.assertIn(installer.GUIDANCE_START, first)
        self.assertFalse(installer.merge_kimi_guidance(path, "second", True))

    def test_malformed_guidance_markers_fail_closed(self) -> None:
        path = self.root / "AGENTS.md"
        path.write_text(installer.GUIDANCE_START, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Malformed"):
            installer.merge_kimi_guidance(path, "test", False)

    def test_duplicate_guidance_markers_fail_closed(self) -> None:
        path = self.root / "AGENTS.md"
        path.write_text(
            f"{installer.GUIDANCE_START}\n{installer.GUIDANCE_END}\n"
            f"{installer.GUIDANCE_START}\n{installer.GUIDANCE_END}\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            installer.merge_kimi_guidance(path, "test", False)

    def test_invalid_json_has_actionable_error(self) -> None:
        path = self.root / "mcp.json"
        path.write_text('{"broken": }', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Invalid JSON.*mcp.json"):
            installer.merge_mcp_config(path, "test", False)

    def test_invalid_json_shapes_fail_closed(self) -> None:
        for value, message in (([], "JSON object"), ({"mcpServers": []}, "mcpServers")):
            path = self.root / f"{len(message)}.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, message):
                installer.merge_mcp_config(path, "test", False)

    def test_atomic_write_preserves_explicit_zero_mode(self) -> None:
        path = self.root / "locked"
        installer._atomic_write(path, "value", 0o000)
        self.assertEqual(0, os.stat(path).st_mode & 0o777)

    def test_skill_link_create_current_and_conflict(self) -> None:
        destination = self.root / "skills" / "agent-jobs"
        self.assertTrue(installer.ensure_skill_link(destination, False))
        self.assertFalse(destination.exists())
        self.assertTrue(installer.ensure_skill_link(destination, True))
        self.assertFalse(installer.ensure_skill_link(destination, False))
        destination.unlink()
        destination.mkdir()
        with self.assertRaisesRegex(FileExistsError, "Refusing"):
            installer.ensure_skill_link(destination, False)

    def test_apply_rolls_back_all_prior_targets_on_failure(self) -> None:
        home = self.root / "home"
        paths = installer._paths(home)
        for name in ("Claude Desktop MCP", "Kimi MCP", "Kimi guidance"):
            path = paths[name]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n" if "MCP" in name else "# Existing\n", encoding="utf-8")
        originals = {name: path.read_bytes() for name, path in paths.items() if path.exists()}
        changes = {name: True for name in paths}
        real_operation = installer._operation
        def fail_on_kimi(name, *args):
            if name == "Kimi guidance":
                raise OSError("synthetic")
            return real_operation(name, *args)
        with patch.object(installer, "_operation", side_effect=fail_on_kimi):
            with self.assertRaisesRegex(OSError, "synthetic"):
                installer.apply_changes(home, changes, "rollback")
        self.assertFalse(paths["shared skill"].exists())
        for name, original in originals.items():
            self.assertEqual(original, paths[name].read_bytes())
            self.assertFalse(installer._backup_path(paths[name], "rollback").exists())

    def test_fresh_install_rollback_removes_new_configs(self) -> None:
        home = self.root / "fresh-home"
        paths = installer._paths(home)
        changes = {name: True for name in paths}
        real_operation = installer._operation
        def fail_on_kimi(name, *args):
            if name == "Kimi guidance":
                raise OSError("synthetic")
            return real_operation(name, *args)
        with patch.object(installer, "_operation", side_effect=fail_on_kimi):
            with self.assertRaisesRegex(OSError, "synthetic"):
                installer.apply_changes(home, changes, "fresh-rollback")
        for path in paths.values():
            self.assertFalse(path.exists())

    def test_backup_collision_is_detected_before_any_write(self) -> None:
        home = self.root / "home"
        paths = installer._paths(home)
        claude = paths["Claude Desktop MCP"]
        claude.parent.mkdir(parents=True)
        claude.write_text("{}\n", encoding="utf-8")
        installer._backup_path(claude, "collision").write_text("existing", encoding="utf-8")
        changes = {name: True for name in paths}
        with self.assertRaisesRegex(FileExistsError, "Backup already exists"):
            installer.apply_changes(home, changes, "collision")
        self.assertEqual("{}\n", claude.read_text(encoding="utf-8"))
        self.assertFalse(paths["shared skill"].exists())

    def test_main_reports_all_targets_when_skill_path_conflicts(self) -> None:
        home = self.root / "home"
        conflict = installer._paths(home)["shared skill"]
        conflict.mkdir(parents=True)
        output = io.StringIO()
        with redirect_stdout(output):
            code = installer.main(["--home", str(home)])
        self.assertEqual(2, code)
        self.assertIn("shared skill: error:", output.getvalue())
        self.assertIn("Claude Desktop MCP: would update", output.getvalue())

    def test_check_returns_one_when_changes_are_pending(self) -> None:
        home = self.root / "home"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(1, installer.main(["--home", str(home), "--check"]))

    def test_codex_toml_merge_preserves_unrelated_configuration(self) -> None:
        path = self.root / "config.toml"
        path.write_text('model = "gpt-5"\n\n[mcp_servers.other]\ncommand = "other"\n', encoding="utf-8")
        self.assertTrue(installer.merge_codex_config(path, "test", True, self.root))
        result = path.read_text(encoding="utf-8")
        self.assertIn('model = "gpt-5"', result)
        self.assertIn('[mcp_servers.other]', result)
        self.assertIn('[mcp_servers.agent-jobs]', result)
        self.assertIn(str(installer.SERVER_PATH), result)
        self.assertIn("startup_timeout_sec = 30.0", result)
        self.assertIn("tool_timeout_sec = 120.0", result)
        self.assertFalse(installer.merge_codex_config(path, "second", True, self.root))

    def test_codex_toml_merge_preserves_existing_timeouts_and_env(self) -> None:
        path = self.root / "config.toml"
        path.write_text(
            '[mcp_servers.agent-jobs]\ncommand = "old"\nargs = ["old"]\n'
            'startup_timeout_sec = 45.0\ntool_timeout_sec = 900.0\n\n'
            '[mcp_servers.agent-jobs.env]\nKEEP = "yes"\n', encoding="utf-8",
        )
        self.assertTrue(installer.merge_codex_config(path, "test", True, self.root))
        result = path.read_text(encoding="utf-8")
        self.assertIn("startup_timeout_sec = 45.0", result)
        self.assertIn("tool_timeout_sec = 900.0", result)
        self.assertIn('KEEP = "yes"', result)
        self.assertFalse(installer.merge_codex_config(path, "second", True, self.root))

    def test_legacy_guidance_section_is_adopted(self) -> None:
        path = self.root / "AGENTS.md"
        path.write_text("## Agent Jobs\n\nOld text.\n\n## Commit Documentation\n\nKeep.\n", encoding="utf-8")
        self.assertTrue(installer.merge_guidance(path, "Codex guidance", "test", True))
        result = path.read_text(encoding="utf-8")
        self.assertIn("Old text.", result)
        self.assertIn("## Commit Documentation", result)
        self.assertIn(installer.GUIDANCE_START, result)
        self.assertFalse(installer.merge_guidance(path, "Codex guidance", "second", True))


if __name__ == "__main__":
    unittest.main()
