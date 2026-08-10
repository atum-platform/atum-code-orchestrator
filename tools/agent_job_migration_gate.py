#!/usr/bin/env python3
"""Evaluate whether one provider can move from CAO canary to promoted routing."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import plistlib
import re
import sqlite3
import time
from typing import Any

from agent_job_supervisor import DB_PATH


SCHEMA = "atum.agent-jobs.migration-gate/v1"
ACCEPTANCE_SCHEMA = "atum.cao.acceptance/v1"
CAO_PROVIDER = {"claude": "claude_code", "kimi": "kimi_cli", "codex": "codex"}
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
DEFAULT_SERVICE_PLIST = (
    Path.home() / "Library/LaunchAgents/com.atum.agent-job-supervisor.plist"
)


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Report is not an object: {path}")
    return value


def _timestamp(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def acceptance_checks(
    paths: list[Path], provider: str, source_commit: str, model: str, now: float
) -> list[dict[str, Any]]:
    reports = [_load(path) for path in paths]
    required_providers = {"mock_cli", CAO_PROVIDER[provider]}
    checks: list[dict[str, Any]] = []
    for required_provider in sorted(required_providers):
        matching = [report for report in reports if report.get("provider") == required_provider]
        reasons: list[str] = []
        if len(matching) != 1:
            reasons.append(f"expected one {required_provider} report, found {len(matching)}")
        else:
            report = matching[0]
            if report.get("schema_version") != ACCEPTANCE_SCHEMA:
                reasons.append("schema mismatch")
            if report.get("mode") != "gate" or report.get("overall") != "passed":
                reasons.append("acceptance gate did not pass")
            if report.get("source_commit") != source_commit:
                reasons.append("source commit mismatch")
            try:
                report_age = now - _timestamp(str(report.get("finished_at") or ""))
                if report_age < -300:
                    reasons.append("report finished_at is in the future")
                elif report_age > 7 * 24 * 3600:
                    reasons.append("report is older than seven days")
            except ValueError:
                reasons.append("invalid finished_at")
            failed_required = [
                item.get("name")
                for item in report.get("checks", [])
                if item.get("required") and item.get("status") != "pass"
            ]
            if failed_required:
                reasons.append("required checks not passed: " + ", ".join(map(str, failed_required)))
            if required_provider != "mock_cli" and model and report.get("model") != model:
                reasons.append("model mismatch")
        checks.append(
            {
                "name": f"acceptance:{required_provider}",
                "status": "pass" if not reasons else "fail",
                "detail": "; ".join(reasons),
            }
        )
    return checks


def acceptance_provenance(paths: list[Path]) -> list[dict[str, Any]]:
    provenance = []
    for path in paths:
        payload = path.read_bytes()
        report = json.loads(payload)
        provenance.append(
            {
                "path": str(path.expanduser().resolve()),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "provider": report.get("provider"),
                "model": report.get("model"),
                "source_commit": report.get("source_commit"),
                "finished_at": report.get("finished_at"),
            }
        )
    return provenance


def deployment_check(plist_path: Path, provider: str, owner_prefix: str) -> dict[str, Any]:
    reasons = []
    environment: dict[str, str] = {}
    try:
        with plist_path.expanduser().open("rb") as handle:
            payload = plistlib.load(handle)
        environment = payload.get("EnvironmentVariables", {})
        if not isinstance(environment, dict):
            raise ValueError("EnvironmentVariables is not a dictionary")
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        reasons.append(f"cannot read deployed service configuration: {exc}")
    if environment:
        providers = {
            item.strip()
            for item in str(environment.get("AGENT_JOB_CAO_CANARY_PROVIDERS", "")).split(",")
            if item.strip()
        }
        prefixes = {
            item.strip()
            for item in str(
                environment.get("AGENT_JOB_CAO_CANARY_OWNER_PREFIXES", "")
            ).split(",")
            if item.strip()
        }
        if environment.get("AGENT_JOB_EXECUTION_BACKEND", "native") != "native":
            reasons.append("deployed default backend is not native")
        if provider not in providers:
            reasons.append("provider is not deployed as a CAO canary")
        if owner_prefix not in prefixes:
            reasons.append("canonical owner prefix is not deployed")
        if not environment.get("AGENT_JOB_CAO_URL"):
            reasons.append("deployed CAO URL is missing")
    return {
        "name": "deployed_canary_configuration",
        "status": "pass" if not reasons else "fail",
        "detail": "; ".join(reasons),
    }


def observation_check(
    db_path: Path,
    provider: str,
    model: str,
    owner_prefix: str,
    min_jobs: int,
    min_hours: float,
    max_failure_rate: float,
    now: float,
) -> dict[str, Any]:
    try:
        db = sqlite3.connect(db_path.expanduser().resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                """SELECT status, failure_kind, created_at, finished_at FROM jobs
                   WHERE execution_backend = 'cao' AND provider = ? AND model = ?
                     AND instr(owner, ?) = 1
                   ORDER BY created_at""",
                (provider, model, owner_prefix),
            ).fetchall()
        finally:
            db.close()
    except sqlite3.Error as exc:
        return {
            "name": "canary_observation",
            "status": "fail",
            "detail": f"cannot read job evidence: {exc}",
            "evidence": {},
        }
    terminal = [row for row in rows if row["status"] in TERMINAL]
    evaluated = [row for row in terminal if row["status"] != "cancelled"]
    failed = [row for row in evaluated if row["status"] in {"failed", "interrupted"}]
    interrupted = [row for row in terminal if row["status"] == "interrupted"]
    completed = [row for row in terminal if row["status"] == "completed"]
    try:
        completed_times = sorted(float(row["finished_at"]) for row in completed)
    except (TypeError, ValueError):
        return {
            "name": "canary_observation",
            "status": "fail",
            "detail": "completed canary evidence has an invalid finished_at",
            "evidence": {"completed_jobs": len(completed)},
        }
    completed_span_hours = (
        (completed_times[-1] - completed_times[0]) / 3600.0
        if len(completed_times) > 1
        else 0.0
    )
    failure_rate = len(failed) / len(evaluated) if evaluated else 1.0
    reasons = []
    if len(completed) < min_jobs:
        reasons.append(f"{len(completed)}/{min_jobs} completed canary jobs")
    if completed_span_hours < min_hours:
        reasons.append(f"{completed_span_hours:.2f}/{min_hours:.2f} completion-span hours")
    if interrupted:
        reasons.append(f"{len(interrupted)} interrupted jobs")
    if failure_rate > max_failure_rate:
        reasons.append(f"failure rate {failure_rate:.3f} exceeds {max_failure_rate:.3f}")
    return {
        "name": "canary_observation",
        "status": "pass" if not reasons else "fail",
        "detail": "; ".join(reasons),
        "evidence": {
            "terminal_jobs": len(terminal),
            "completed_jobs": len(completed),
            "failed_jobs": len(failed),
            "interrupted_jobs": len(interrupted),
            "failure_rate": failure_rate,
            "completed_span_hours": completed_span_hours,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=sorted(CAO_PROVIDER), required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--acceptance-report", action="append", type=Path)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--service-plist", type=Path, default=DEFAULT_SERVICE_PLIST)
    parser.add_argument("--min-jobs", type=int, default=5)
    parser.add_argument("--min-hours", type=float, default=24.0)
    parser.add_argument("--max-failure-rate", type=float, default=0.1)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.min_jobs < 5:
        parser.error("--min-jobs cannot weaken the five-job baseline")
    if args.min_hours < 24:
        parser.error("--min-hours cannot weaken the 24-hour baseline")
    if not 0 <= args.max_failure_rate <= 0.1:
        parser.error("--max-failure-rate cannot weaken the 0.1 baseline")
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", args.source_commit):
        parser.error("--source-commit must be a full lowercase Git object ID")
    owner_prefix = f"cao-canary:{args.source_commit}:"
    now = time.time()
    acceptance_paths = args.acceptance_report or []
    checks = acceptance_checks(
        acceptance_paths, args.provider, args.source_commit, args.model, now
    )
    checks.append(deployment_check(args.service_plist, args.provider, owner_prefix))
    checks.append(
        observation_check(
            args.db,
            args.provider,
            args.model,
            owner_prefix,
            args.min_jobs,
            args.min_hours,
            args.max_failure_rate,
            now,
        )
    )
    passed = all(check["status"] == "pass" for check in checks)
    result = {
        "schema_version": SCHEMA,
        "provider": args.provider,
        "source_commit": args.source_commit,
        "model": args.model,
        "owner_prefix": owner_prefix,
        "database": str(args.db.expanduser().resolve()),
        "service_plist": str(args.service_plist.expanduser().resolve()),
        "acceptance_reports": acceptance_provenance(acceptance_paths),
        "thresholds": {
            "min_jobs": args.min_jobs,
            "min_hours": args.min_hours,
            "max_failure_rate": args.max_failure_rate,
        },
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "verdict": "promote" if passed else "hold",
        "checks": checks,
    }
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
