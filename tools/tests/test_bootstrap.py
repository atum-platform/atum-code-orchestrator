from __future__ import annotations

from contextlib import redirect_stderr
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import bootstrap  # noqa: E402


class BootstrapBoundaryTest(unittest.TestCase):
    def test_bootstrap_only_installs_coding_clients(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            venv = Path(directory) / ".venv"
            python = venv / "bin/python"
            python.parent.mkdir(parents=True)
            python.touch()
            with (
                patch.object(bootstrap, "VENV", venv),
                patch.object(bootstrap, "run", return_value=0) as run,
                patch.object(sys, "argv", ["bootstrap.py"]),
            ):
                self.assertEqual(0, bootstrap.main())

        commands = [call.args[1] for call in run.call_args_list if len(call.args) > 1]
        self.assertEqual(
            [
                "-m",
                "tools/install_agent_job_clients.py",
                "tools/install_agent_job_supervisor.py",
                "tools/install_agent_job_clients.py",
                "tools/agent_job_client.py",
            ],
            commands,
        )
        self.assertFalse(any("hermes" in str(call).lower() for call in run.call_args_list))

    def test_retired_hermes_option_is_rejected(self) -> None:
        with patch.object(sys, "argv", ["bootstrap.py", "--with-hermes"]):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                bootstrap.main()
        self.assertEqual(2, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
