#!/usr/bin/env python3
"""Create the local runtime and wire agent-jobs into installed coding clients."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"


def run(*args: str, allowed_codes: tuple[int, ...] = (0,)) -> int:
    result = subprocess.run(args, cwd=ROOT, check=False)
    if result.returncode not in allowed_codes:
        raise subprocess.CalledProcessError(result.returncode, args)
    return result.returncode


def main() -> int:
    if sys.version_info < (3, 10):
        for candidate in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3"):
            if Path(candidate).is_file():
                os.execv(candidate, [candidate, str(Path(__file__).resolve()), *sys.argv[1:]])
        raise SystemExit("Python 3.10+ is required; install it with Homebrew and rerun bootstrap.py")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify bindings without changing them")
    parser.add_argument("--with-hermes", action="store_true", help="also update existing Hermes profiles")
    args = parser.parse_args()
    stable_root = Path.home() / ".local/share/atum-agent-jobs"
    if args.with_hermes and ROOT.resolve() != stable_root.resolve():
        raise SystemExit(f"Hermes integration requires the stable checkout path: {stable_root}")
    if args.check:
        python = VENV / "bin/python"
        if not python.is_file():
            raise SystemExit("No local runtime exists; run bootstrap.py once before --check")
        code = run(str(python), "tools/install_agent_job_clients.py", "--check", allowed_codes=(0, 1))
        run(str(python), "tools/agent_job_client.py", "ping")
        return code
    if not VENV.joinpath("bin/python").is_file():
        run(sys.executable, "-m", "venv", str(VENV))
    python = str(VENV / "bin/python")
    run(python, "-m", "pip", "install", "-r", "requirements.txt")
    run(python, "tools/install_agent_job_clients.py")
    if args.with_hermes and Path.home().joinpath(".hermes/profiles").is_dir():
        run(python, "tools/install_hermes_profiles.py")
    run(python, "tools/install_agent_job_supervisor.py", "install")
    run(python, "tools/install_agent_job_clients.py", "--apply")
    if args.with_hermes and Path.home().joinpath(".hermes/profiles").is_dir():
        run(python, "tools/install_hermes_profiles.py", "--apply")
    run(python, "tools/agent_job_client.py", "ping")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
