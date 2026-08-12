from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

from agent_quota_broker import (  # noqa: E402
    evaluate_health,
    rate_limit_cooldown,
    rebalance_default_route,
)


class AgentQuotaBrokerTest(unittest.TestCase):
    def write_history(
        self, root: Path, provider: str, used: float, captured: float,
        reset: float, window_minutes: int = 300,
    ) -> None:
        root.mkdir(parents=True, exist_ok=True)
        iso = lambda value: datetime.fromtimestamp(value, timezone.utc).isoformat()
        payload = {
            "version": 1,
            "preferredAccountKey": "account",
            "accounts": {
                "account": [{
                    "name": "session",
                    "windowMinutes": window_minutes,
                    "entries": [{
                        "capturedAt": iso(captured),
                        "resetsAt": iso(reset),
                        "usedPercent": used,
                    }],
                }],
            },
        }
        (root / f"{provider}.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_fresh_projected_pressure_and_hysteresis(self) -> None:
        now = 10_000.0
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"AGENT_JOB_QUOTA_HISTORY_DIR": temporary}
        ):
            root = Path(temporary)
            self.write_history(root, "claude", 50, now - 30, now + 9000)
            pressured = evaluate_health("claude", now)
            self.assertEqual("pressured", pressured["state"])

            self.write_history(root, "claude", 60, now - 30, now + 3570)
            held = evaluate_health("claude", now, previous_state="pressured")
            self.assertEqual("pressured", held["state"])

            self.write_history(root, "claude", 20, now - 30, now + 3570)
            recovered = evaluate_health("claude", now, previous_state="pressured")
            self.assertEqual("available", recovered["state"])

    def test_stale_or_missing_telemetry_is_explicit(self) -> None:
        now = 20_000.0
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {
                "AGENT_JOB_QUOTA_HISTORY_DIR": temporary,
                "AGENT_JOB_QUOTA_STALE_SECONDS": "300",
            },
        ):
            root = Path(temporary)
            self.write_history(root, "codex", 20, now - 301, now + 1000)
            self.assertEqual("stale", evaluate_health("codex", now)["state"])
            self.assertEqual("unknown", evaluate_health("kimi", now)["state"])

    def test_active_failure_cooldown_overrides_cache(self) -> None:
        health = evaluate_health("kimi", 100, cooldown_until=200)
        self.assertEqual("rate_limited", health["state"])
        self.assertEqual(200, health["cooldown_until"])

    def test_rate_limit_detection_uses_reset_evidence_or_default(self) -> None:
        limited, cooldown, evidence = rate_limit_cooldown(
            "429 rate limit; try again in 2 hours", 100
        )
        self.assertTrue(limited)
        self.assertEqual(7_300, cooldown)
        self.assertIn("2 hours", evidence)
        self.assertEqual((False, None, ""), rate_limit_cooldown("syntax error", 100))

    def test_rebalance_changes_only_default_agent_job_routes(self) -> None:
        decision = {
            "lane": "agent_jobs", "provider": "claude", "model_alias": "claude_deep",
            "fallback_provider": "kimi", "fallback_model_alias": "kimi_standard",
            "reasons": ["static"],
        }
        result = rebalance_default_route(decision, {
            "claude": {"state": "pressured"},
            "kimi": {"state": "unknown"},
        })
        self.assertEqual("kimi", result["provider"])
        self.assertEqual("claude", result["fallback_provider"])
        self.assertIn("quota broker", result["reasons"][-2])

        direct = {**decision, "lane": "direct"}
        self.assertEqual(direct, rebalance_default_route(direct, {}))


if __name__ == "__main__":
    unittest.main()
