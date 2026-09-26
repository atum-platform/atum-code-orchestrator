from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid


SCRIPT = Path(__file__).resolve().parents[1] / "agent_job_check_server.py"


def load_broker(
    workdir: Path, runtime: Path, checks: list[dict[str, object]],
    deny_read: object = None,
):
    with patch.dict(os.environ, {
        "ACO_CHECKS_WORKDIR": str(workdir),
        "ACO_CHECKS_RUNTIME": str(runtime),
        "ACO_CHECKS_JSON": json.dumps(checks),
        "ACO_CHECKS_DENY_READ": json.dumps([] if deny_read is None else deny_read),
    }):
        spec = importlib.util.spec_from_file_location(f"check_broker_{uuid.uuid4().hex}", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


@unittest.skipUnless(os.uname().sysname == "Darwin", "macOS sandbox-exec is required")
class ApprovedCheckBrokerTest(unittest.TestCase):
    def test_unknown_name_never_becomes_command_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workdir = root / "work"
            runtime = root / "runtime"
            workdir.mkdir()
            runtime.mkdir()
            broker = load_broker(workdir, runtime, [
                {"name": "unit", "argv": ["/usr/bin/true"], "timeout_seconds": 5}
            ])

            result = broker._run("/bin/sh -c 'touch escaped'")

            self.assertFalse(result["ok"])
            self.assertEqual(["unit"], result["available"])
            self.assertFalse((workdir / "escaped").exists())

    def test_exact_check_is_confined_and_provider_credentials_are_scrubbed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"ANTHROPIC_API_KEY": "must-not-leak"}, clear=False
        ):
            root = Path(temporary)
            workdir = root / "work"
            runtime = root / "runtime"
            workdir.mkdir()
            runtime.mkdir()
            (workdir / ".git").mkdir()
            command = (
                'printf "%s" "$ANTHROPIC_API_KEY"; '
                'touch inside; touch .git/config; touch ../outside'
            )
            broker = load_broker(workdir, runtime, [
                {"name": "probe", "argv": ["/bin/sh", "-c", command], "timeout_seconds": 5}
            ])

            result = broker._run("probe")

            self.assertFalse(result["ok"])
            self.assertEqual("", result["stdout"])
            self.assertTrue((workdir / "inside").exists())
            self.assertFalse((workdir / ".git/config").exists())
            self.assertFalse((root / "outside").exists())

    def test_named_credentials_are_unreadable_but_runtime_stays_usable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            state = root / "state"
            runtime = state / "runtime" / "job"
            workdir = root / "work"
            runtime.mkdir(parents=True)
            workdir.mkdir()
            (state / "opencode.env").write_text("OPENCODE_API_KEY=state-secret\n")
            profile = root / "profile.env"
            profile.write_text("ANTHROPIC_API_KEY=profile-secret\n")
            alias = root / "alias"
            alias.symlink_to(root)
            sibling = state / "runtime" / "other-job"
            sibling.mkdir()
            (sibling / "check-unit.stdout").write_text("sibling-secret\n")
            # The runtime is writable and re-allowed inside the denied state
            # directory, so links from it must not reach denied files.
            command = (
                f'cat "{state}/opencode.env" "{profile}" "{sibling}/check-unit.stdout"; '
                f'ln -s "{state}/opencode.env" "{runtime}/symlink"; cat "{runtime}/symlink"; '
                f'ln "{state}/opencode.env" "{runtime}/hardlink"; cat "{runtime}/hardlink"; '
                f'ln -s "{profile}" profile-link; cat profile-link; '
                f'cp "{state}/opencode.env" "{runtime}/copy"; cat "{runtime}/copy"; '
                f'echo runtime-ok > "{runtime}/probe"; cat "{runtime}/probe"; '
                'echo work-ok > seen; cat seen'
            )
            broker = load_broker(
                workdir, runtime,
                [{"name": "probe", "argv": ["/bin/sh", "-c", command], "timeout_seconds": 5}],
                # An aliased path (through a symlinked parent) is still denied.
                deny_read=[str(alias / "state"), str(profile)],
            )

            result = broker._run("probe")

            output = f"{result['stdout']}{result['stderr']}"
            self.assertNotIn("secret", output)
            self.assertIn("runtime-ok", result["stdout"])
            self.assertIn("work-ok", result["stdout"])

    def test_malformed_read_denials_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "work").mkdir()
            (root / "runtime").mkdir()
            for denials in (["relative/state"], [7], {"path": "/tmp"}):
                with self.subTest(denials=denials), self.assertRaisesRegex(
                    RuntimeError, "absolute paths"
                ):
                    load_broker(root / "work", root / "runtime", [], deny_read=denials)
            with patch.dict(os.environ, {"ACO_CHECKS_DENY_READ": "not json"}), \
                    self.assertRaises(ValueError):
                spec = importlib.util.spec_from_file_location("broken_broker", SCRIPT)
                assert spec is not None and spec.loader is not None
                with patch.dict(os.environ, {
                    "ACO_CHECKS_WORKDIR": str(root / "work"),
                    "ACO_CHECKS_RUNTIME": str(root / "runtime"),
                }):
                    spec.loader.exec_module(importlib.util.module_from_spec(spec))

    def test_network_and_timeout_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workdir = root / "work"
            runtime = root / "runtime"
            workdir.mkdir()
            runtime.mkdir()
            broker = load_broker(workdir, runtime, [
                {
                    "name": "network",
                    "argv": ["/usr/bin/curl", "-sS", "--max-time", "2", "https://example.com"],
                    "timeout_seconds": 5,
                },
                {"name": "slow", "argv": ["/bin/sleep", "30"], "timeout_seconds": 1},
            ])

            network = broker._run("network")
            slow = broker._run("slow")

            self.assertFalse(network["ok"])
            self.assertFalse(slow["ok"])
            self.assertTrue(slow["timed_out"])

    def test_external_cleanup_terminates_active_check_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workdir = root / "work"
            runtime = root / "runtime"
            workdir.mkdir()
            runtime.mkdir()
            broker = load_broker(workdir, runtime, [
                {"name": "slow", "argv": ["/bin/sleep", "30"], "timeout_seconds": 30}
            ])
            result: dict[str, object] = {}

            thread = threading.Thread(target=lambda: result.update(broker._run("slow")))
            thread.start()
            deadline = time.monotonic() + 5
            while not (runtime / "check-slow.process.json").exists():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)

            broker._stop_active_process()
            thread.join(timeout=5)

            self.assertFalse(thread.is_alive())
            self.assertFalse(result["ok"])
            self.assertFalse((runtime / "check-slow.process.json").exists())


if __name__ == "__main__":
    unittest.main()
