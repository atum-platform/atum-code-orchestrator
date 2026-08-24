from __future__ import annotations

from pathlib import Path
import sys
import unittest


TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

import agent_job_policy  # noqa: E402


class AgentJobPolicyTest(unittest.TestCase):
    def test_project_roots_accept_common_casing(self) -> None:
        home = Path("/Users/tester")

        roots = agent_job_policy.default_allowed_roots(home)

        self.assertIn(home / "Projects", roots)
        self.assertIn(home / "projects", roots)

    def test_default_roots_exclude_hermes_owned_paths(self) -> None:
        roots = agent_job_policy.default_allowed_roots(Path("/Users/tester"))

        self.assertFalse(any(".hermes" in path.parts for path in roots))


if __name__ == "__main__":
    unittest.main()
