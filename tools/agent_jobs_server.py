#!/usr/bin/env python3
"""Thin typed MCP binding for durable read-only cross-agent jobs."""

from __future__ import annotations

import asyncio
import json

from mcp.server.fastmcp import FastMCP

import review_core


mcp = FastMCP("agent-jobs")


@mcp.tool()
async def route_decide(
    caller_provider: str,
    surface: str,
    capability: str,
    protocol_version: int = 1,
    complexity: str = "standard",
    risk: str = "medium",
    scope: str = "single_module",
    duration: str = "medium",
    durability: str = "session",
    parallelizable: bool = False,
    surface_capabilities: dict[str, bool] | None = None,
    explicit_provider: str = "",
    explicit_model: str = "",
    session_id: str = "",
    owner: str = "",
) -> str:
    """Decide a route; pass protocol v2 capabilities for deterministic degradation."""
    result = await asyncio.to_thread(
        review_core.routing_decide,
        protocol_version=protocol_version, caller_provider=caller_provider,
        surface=surface, capability=capability, complexity=complexity, risk=risk,
        scope=scope, duration=duration, durability=durability,
        parallelizable=parallelizable,
        surface_capabilities=surface_capabilities or {},
        explicit_provider=explicit_provider, explicit_model=explicit_model,
        session_id=session_id, owner=owner,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def route_feedback(decision_id: str, session_id: str, outcome: str) -> str:
    """Close one routing decision; identical retries are idempotent."""
    result = await asyncio.to_thread(
        review_core.routing_feedback, decision_id, session_id, outcome
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def route_reconcile(session_id: str, active_decision_ids: list[str]) -> str:
    """Release this session's native-worker leases absent from the active set."""
    result = await asyncio.to_thread(
        review_core.routing_reconcile, session_id, active_decision_ids
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def route_status() -> str:
    """Report native reservation states and decision-to-feedback join rate."""
    result = await asyncio.to_thread(review_core.routing_status)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def job_submit(
    provider: str,
    instructions: str,
    workdir: str,
    model: str = "",
    context_git_diff: bool = False,
    context_git_base: str = "HEAD",
    context_files: list[str] | None = None,
    context_text: str = "",
    expected_output: str = "",
    timeout_seconds: int = 1800,
    max_turns: int = 0,
    idempotency_key: str = "",
    label: str = "",
    owner: str = "",
) -> str:
    """Submit a durable read-only job. Kimi defaults to K3; max_turns=0 omits its ceiling."""
    result = await asyncio.to_thread(
        review_core.job_submit,
        provider=provider, model=model, instructions=instructions, workdir=workdir,
        context_git_diff=context_git_diff, context_git_base=context_git_base,
        context_files=context_files, context_text=context_text,
        expected_output=expected_output, timeout_seconds=timeout_seconds,
        max_turns=max_turns, idempotency_key=idempotency_key, label=label, owner=owner,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def job_read(
    job_id: str,
    cursor: int = 0,
    max_bytes: int = 64_000,
    wait_seconds: int = 0,
    event_cursor: int | None = None,
) -> str:
    """Read incremental output, normalized events, and status; optionally wait 60 seconds."""
    result = await asyncio.to_thread(
        review_core.job_read, job_id, cursor, max_bytes, wait_seconds, event_cursor
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def job_list(status: str = "", limit: int = 50, owner: str = "") -> str:
    """List durable jobs across sessions, optionally filtered by status or owner."""
    result = await asyncio.to_thread(review_core.job_list, status, limit, owner)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def job_cancel(job_id: str) -> str:
    """Request cancellation of a durable job."""
    result = await asyncio.to_thread(review_core.job_cancel, job_id)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def job_inbox(
    owner: str, limit: int = 20, ack_delivery_ids: list[str] | None = None
) -> str:
    """Read and acknowledge owner-scoped terminal-job deliveries at least once."""
    result = await asyncio.to_thread(
        review_core.job_inbox, owner, limit, ack_delivery_ids
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
