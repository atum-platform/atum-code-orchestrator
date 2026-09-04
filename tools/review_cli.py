#!/usr/bin/env python3
"""CLI binding for the same guarded read-only review operations as MCP."""

from __future__ import annotations

import argparse
import json
import sys

import review_core


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    submit = sub.add_parser("submit")
    submit.add_argument("--provider", choices=sorted(review_core.PROVIDERS), required=True)
    submit.add_argument("--model", default="")
    submit.add_argument("--instructions", required=True)
    submit.add_argument("--workdir", required=True)
    submit.add_argument("--context-git-diff", action="store_true")
    submit.add_argument("--context-git-base", default="HEAD")
    submit.add_argument("--context-file", action="append", dest="context_files")
    submit.add_argument("--context-text", default="")
    submit.add_argument("--expected-output", default="")
    submit.add_argument("--queue-timeout-seconds", type=int, default=900)
    submit.add_argument("--run-timeout-seconds", type=int, default=5400)
    submit.add_argument(
        "--timeout-seconds", type=int,
        help="deprecated alias for --run-timeout-seconds",
    )
    submit.add_argument(
        "--max-turns", type=int, default=0,
        help="deprecated compatibility option; accepted but ignored",
    )
    submit.add_argument("--idempotency-key", default="")
    submit.add_argument("--label", default="")
    submit.add_argument("--owner", default="")
    read = sub.add_parser("read")
    read.add_argument("job_id")
    read.add_argument("--cursor", type=int, default=0)
    read.add_argument("--event-cursor", type=int)
    read.add_argument("--max-bytes", type=int, default=64_000)
    read.add_argument("--wait-seconds", type=int, default=0)
    listing = sub.add_parser("list")
    listing.add_argument("--status", default="")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--owner", default="")
    cancel = sub.add_parser("cancel")
    cancel.add_argument("job_id")
    inbox = sub.add_parser("inbox")
    inbox.add_argument("--owner", required=True)
    inbox.add_argument("--limit", type=int, default=20)
    inbox.add_argument("--ack-delivery-id", action="append", dest="ack_delivery_ids")
    route = sub.add_parser("route-decide")
    route.add_argument("--protocol-version", type=int, default=1)
    route.add_argument("--caller-provider", required=True)
    route.add_argument("--surface", required=True)
    route.add_argument("--capability", required=True)
    route.add_argument("--complexity", default="standard")
    route.add_argument("--risk", default="medium")
    route.add_argument("--scope", default="single_module")
    route.add_argument("--duration", default="medium")
    route.add_argument("--durability", default="session")
    route.add_argument("--parallelizable", action="store_true")
    route.add_argument("--surface-capabilities", type=json.loads, default={})
    route.add_argument("--explicit-provider", default="")
    route.add_argument("--explicit-model", default="")
    route.add_argument("--session-id", default="")
    route.add_argument("--previous-decision-id", default="")
    route.add_argument(
        "--escalation-reason",
        choices=("provider_failure", "rate_limit", "unusable_output", "scope_growth", "capability_mismatch"),
        default="",
    )
    route.add_argument("--escalation-evidence", default="")
    route.add_argument("--owner", default="")
    feedback = sub.add_parser("route-feedback")
    feedback.add_argument("decision_id")
    feedback.add_argument("--session-id", required=True)
    feedback.add_argument(
        "--outcome",
        choices=("completed", "failed", "abandoned", "escalated", "not_started"),
        required=True,
    )
    reconcile = sub.add_parser("route-reconcile")
    reconcile.add_argument("--session-id", required=True)
    reconcile.add_argument("--active-decision-id", action="append", default=[])
    sub.add_parser("route-status")
    return parser


def dispatch(values: dict[str, object]) -> dict[str, object]:
    action = str(values.pop("action"))
    if action == "submit":
        return review_core.job_submit(**values)  # type: ignore[arg-type]
    if action == "route-decide":
        return review_core.routing_decide(**values)  # type: ignore[arg-type]
    if action == "route-feedback":
        return review_core.routing_feedback(**values)  # type: ignore[arg-type]
    if action == "route-reconcile":
        values["active_decision_ids"] = values.pop("active_decision_id")
        return review_core.routing_reconcile(**values)  # type: ignore[arg-type]
    if action == "route-status":
        return review_core.routing_status()
    if action == "read":
        return review_core.job_read(**values)  # type: ignore[arg-type]
    if action == "list":
        return review_core.job_list(**values)  # type: ignore[arg-type]
    if action == "inbox":
        return review_core.job_inbox(**values)  # type: ignore[arg-type]
    return review_core.job_cancel(**values)  # type: ignore[arg-type]


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if args.action == "submit" and args.provider != "kimi" and not args.model:
        parser.error("--model is required unless --provider=kimi")
    try:
        result = dispatch(vars(args))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
