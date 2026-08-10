#!/usr/bin/env python3
"""Pure provider-event normalization for durable agent jobs."""

from __future__ import annotations

import codecs
import hashlib
import json
from typing import Any


MAX_EVENT_TEXT_CHARS = 16_000
MAX_EVENT_RECORD_BYTES = (MAX_EVENT_TEXT_CHARS * 4) + 4096
MAX_COLLECTION_ITEMS = 50
MAX_VALUE_DEPTH = 4
MAX_CLAUDE_STREAM_BLOCK_CHARS = 1_048_576
MAX_CLAUDE_STREAM_BLOCKS = 256
MAX_CLAUDE_SNAPSHOT_MESSAGES = 64
PRIVATE_STDOUT_PROVIDERS = {"claude", "kimi"}


def _bounded(value: Any, depth: int = 0) -> Any:
    if depth >= MAX_VALUE_DEPTH:
        return "[nested value omitted]"
    if isinstance(value, str):
        if len(value) <= MAX_EVENT_TEXT_CHARS:
            return value
        return value[:MAX_EVENT_TEXT_CHARS] + "[truncated]"
    if isinstance(value, dict):
        return {
            str(key)[:200]: _bounded(item, depth + 1)
            for key, item in list(value.items())[:MAX_COLLECTION_ITEMS]
        }
    if isinstance(value, list):
        return [_bounded(item, depth + 1) for item in value[:MAX_COLLECTION_ITEMS]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_EVENT_TEXT_CHARS]


def _raw_payload(value: Any) -> dict[str, Any]:
    bounded = _bounded(value)
    encoded = json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= MAX_EVENT_TEXT_CHARS:
        return {"provider_event": bounded}
    preview = encoded.encode("utf-8")[:MAX_EVENT_TEXT_CHARS].decode(
        "utf-8", errors="ignore"
    )
    return {"provider_event_preview": preview, "truncated": True}


def bound_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    bounded = _bounded(payload)
    encoded = json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= MAX_EVENT_RECORD_BYTES - 4096:
        return bounded
    preview = encoded.encode("utf-8")[:MAX_EVENT_TEXT_CHARS].decode(
        "utf-8", errors="ignore"
    )
    return {"payload_preview": preview, "truncated": True}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _tool_name(item: dict[str, Any]) -> str:
    item_type = str(item.get("type") or "tool")
    if item_type == "command_execution":
        return "command"
    if item_type == "mcp_tool_call":
        server = str(item.get("server") or item.get("server_name") or "mcp")
        tool = str(item.get("tool") or item.get("tool_name") or "tool")
        return f"{server}:{tool}"[:200]
    if item_type == "web_search":
        return "web_search"
    return item_type[:200]


def _codex_events(value: dict[str, Any]) -> list[dict[str, Any]]:
    event_type = str(value.get("type") or "")
    item = value.get("item")
    item = item if isinstance(item, dict) else {}
    item_type = str(item.get("type") or "")
    item_id = str(item.get("id") or "")[:200]

    if event_type == "turn.started":
        return [{"kind": "turn_started", "payload": {}}]
    if event_type == "turn.completed":
        usage = value.get("usage")
        return [{"kind": "usage", "payload": {"usage": _bounded(usage or {})}}]
    if event_type in {"turn.failed", "error"}:
        message = _text(value.get("message") or item.get("message"))
        return [{"kind": "warning", "payload": {"message": message[:MAX_EVENT_TEXT_CHARS]}}]
    if event_type == "item.started":
        if item_type in {"command_execution", "mcp_tool_call", "web_search"}:
            return [{
                "kind": "tool_started",
                "payload": {"id": item_id, "name": _tool_name(item)},
            }]
        if item_type == "reasoning":
            text = _text(item.get("text"))
            return [{"kind": "thinking_delta", "payload": {"text": text}}]
        return [{"kind": "provider_raw", "payload": _raw_payload(value)}]
    if event_type == "item.completed":
        if item_type == "agent_message":
            return [{
                "kind": "message_delta",
                "payload": {"text": _text(item.get("text"))},
            }]
        if item_type == "reasoning":
            return [{
                "kind": "thinking_delta",
                "payload": {"text": _text(item.get("text"))},
            }]
        if item_type in {"command_execution", "mcp_tool_call", "web_search"}:
            return [{
                "kind": "tool_finished",
                "payload": {
                    "id": item_id,
                    "name": _tool_name(item),
                    "status": str(item.get("status") or "completed")[:100],
                    "exit_code": item.get("exit_code"),
                },
            }]
        if item_type == "error":
            return [{
                "kind": "warning",
                "payload": {"message": _text(item.get("message"))[:MAX_EVENT_TEXT_CHARS]},
            }]
        return [{"kind": "progress", "payload": {"item": _bounded(item)}}]
    return [{"kind": "provider_raw", "payload": _raw_payload(value)}]


def _claude_metadata(value: dict[str, Any]) -> dict[str, Any]:
    event = value.get("event")
    event = event if isinstance(event, dict) else {}
    payload = {
        "type": str(value.get("type") or "unknown")[:100],
        "subtype": str(value.get("subtype") or "")[:100],
        "event_type": str(event.get("type") or "")[:100],
    }
    return {key: item for key, item in payload.items() if item}


def _content_bytes(value: Any) -> int:
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if value is None:
        return 0
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        return len(str(value).encode("utf-8"))


def _collection_count(value: Any) -> int:
    return len(value) if isinstance(value, (dict, list)) else 0


def _remember_tool(state: dict[str, Any], tool_id: str, name: str) -> None:
    tools = state.setdefault("tools", {})
    if tool_id not in tools and len(tools) >= 256:
        return
    tools[tool_id] = name


def _claude_snapshot_key(
    state: dict[str, Any], message_id: str, block: dict[str, Any]
) -> str:
    ordinals = state.setdefault("snapshot_ordinals", {})
    if message_id not in ordinals and len(ordinals) >= MAX_CLAUDE_SNAPSHOT_MESSAGES:
        oldest = next(iter(ordinals))
        del ordinals[oldest]
        prefix = f"{oldest}:"
        state["snapshot_seen"] = {
            key for key in state.setdefault("snapshot_seen", set())
            if not key.startswith(prefix)
        }
        state.setdefault("snapshot_previous", {}).pop(oldest, None)
    fingerprint = hashlib.sha256(
        json.dumps(block, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    previous = state.setdefault("snapshot_previous", {}).get(message_id)
    if previous and previous[0] == fingerprint:
        return str(previous[1])
    ordinal = int(ordinals.get(message_id, 0))
    ordinals[message_id] = ordinal + 1
    key = f"{message_id}:{ordinal}"
    state["snapshot_previous"][message_id] = (fingerprint, key)
    return key


def _claude_stream_suffix(
    state: dict[str, Any], block_type: str, text: str
) -> str:
    if state.get("stream_tracking_overflow"):
        return ""
    streamed = state.setdefault("streamed_content", {})
    consumed = state.setdefault("streamed_consumed", set())
    matches = [
        (len(content), index)
        for index, item in streamed.items()
        if index not in consumed
        and item.get("type") == block_type
        and (content := _text(item.get("text")))
        and text.startswith(content)
    ]
    if not matches:
        return text
    length, index = max(matches)
    consumed.add(index)
    if index in state.setdefault("streamed_overflow", set()):
        return ""
    return text[length:]


def _claude_remember_stream_text(
    state: dict[str, Any], index: str, block_type: str, text: str
) -> None:
    streamed = state.setdefault("streamed_content", {})
    if index not in streamed and len(streamed) >= MAX_CLAUDE_STREAM_BLOCKS:
        state["stream_tracking_overflow"] = True
        return
    item = streamed.setdefault(index, {"type": block_type, "text": ""})
    if item.get("type") != block_type:
        return
    current = _text(item.get("text"))
    remaining = MAX_CLAUDE_STREAM_BLOCK_CHARS - len(current)
    if remaining <= 0 or len(text) > remaining:
        state.setdefault("streamed_overflow", set()).add(index)
    if remaining > 0:
        item["text"] = current + text[:remaining]


def _claude_message_event(state: dict[str, Any], text: str) -> dict[str, Any]:
    state["top_level_text_chars"] = int(state.get("top_level_text_chars", 0)) + len(text)
    return {"kind": "message_delta", "payload": {"text": text}}


def _claude_events(value: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    record_type = str(value.get("type") or "")
    subtype = str(value.get("subtype") or "")
    events: list[dict[str, Any]] = []

    if value.get("parent_tool_use_id") is not None and record_type in {
        "stream_event", "assistant", "user",
    }:
        return []

    if record_type == "system":
        if subtype == "init":
            tools = value.get("tools")
            mcp_servers = value.get("mcp_servers")
            return [{
                "kind": "progress",
                "payload": {
                    "phase": "initialized",
                    "model": str(value.get("model") or "")[:200],
                    "permission_mode": str(
                        value.get("permissionMode") or value.get("permission_mode") or ""
                    )[:100],
                    "claude_code_version": str(value.get("claude_code_version") or "")[:100],
                    "tool_count": _collection_count(tools),
                    "mcp_server_count": _collection_count(mcp_servers),
                },
            }]
        if subtype == "status" and value.get("status") == "requesting":
            return [{"kind": "waiting", "payload": {"reason": "provider_request"}}]
        if subtype == "thinking_tokens":
            return []
        return [{"kind": "progress", "payload": _claude_metadata(value)}]

    if record_type == "stream_event":
        event = value.get("event")
        event = event if isinstance(event, dict) else {}
        event_type = str(event.get("type") or "")
        if event_type == "message_start":
            state["blocks"] = {}
            state["streamed_content"] = {}
            state["streamed_consumed"] = set()
            state["streamed_overflow"] = set()
            state["stream_tracking_overflow"] = False
            message = event.get("message")
            message = message if isinstance(message, dict) else {}
            state["stream_message_id"] = str(message.get("id") or "")[:200]
            return [{
                "kind": "turn_started",
                "payload": {"model": str(message.get("model") or "")[:200]},
            }]
        if event_type == "content_block_start":
            block = event.get("content_block")
            block = block if isinstance(block, dict) else {}
            index = str(event.get("index") or 0)
            block_type = str(block.get("type") or "")
            state.setdefault("blocks", {})[index] = {
                "type": block_type,
                "id": str(block.get("id") or "")[:200],
                "name": str(block.get("name") or "tool")[:200],
            }
            if block_type == "tool_use":
                tool_id = str(block.get("id") or "")[:200]
                tool_name = str(block.get("name") or "tool")[:200]
                _remember_tool(state, tool_id, tool_name)
                return [{
                    "kind": "tool_started",
                    "payload": {"id": tool_id, "name": tool_name},
                }]
            return []
        if event_type == "content_block_delta":
            delta = event.get("delta")
            delta = delta if isinstance(delta, dict) else {}
            delta_type = str(delta.get("type") or "")
            index = str(event.get("index") or 0)
            if delta_type == "text_delta":
                text = _text(delta.get("text"))
                if text:
                    _claude_remember_stream_text(state, index, "text", text)
                    return [_claude_message_event(state, text)]
                return []
            if delta_type == "thinking_delta":
                thinking = _text(delta.get("thinking"))
                if thinking:
                    _claude_remember_stream_text(state, index, "thinking", thinking)
                    return [{"kind": "thinking_delta", "payload": {"text": thinking}}]
                return []
            if delta_type == "input_json_delta":
                block = state.setdefault("blocks", {}).get(index, {})
                return [{
                    "kind": "progress",
                    "payload": {
                        "phase": "tool_input",
                        "tool_use_id": str(block.get("id") or "")[:200],
                        "input_bytes": _content_bytes(delta.get("partial_json")),
                    },
                }]
            # Thinking signatures deliberately stay in raw logs only.
            return []
        if event_type == "message_delta":
            usage = event.get("usage")
            delta = event.get("delta")
            delta = delta if isinstance(delta, dict) else {}
            if isinstance(usage, dict):
                events.append({
                    "kind": "usage",
                    "payload": {
                        "scope": "message",
                        "usage": _bounded(usage),
                        "stop_reason": str(delta.get("stop_reason") or "")[:100],
                    },
                })
            return events or [{"kind": "progress", "payload": {"phase": "message_delta"}}]
        if event_type in {"content_block_stop", "message_stop"}:
            return []
        return [{"kind": "progress", "payload": _claude_metadata(value)}]

    if record_type == "assistant":
        message = value.get("message")
        message = message if isinstance(message, dict) else {}
        message_id = str(message.get("id") or "")[:200]
        content = message.get("content")
        content = content if isinstance(content, list) else []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "")
            key = _claude_snapshot_key(state, message_id, block)
            if key in state.setdefault("snapshot_seen", set()):
                continue
            state["snapshot_seen"].add(key)
            if block_type == "text":
                text = _claude_stream_suffix(
                    state, block_type, _text(block.get("text"))
                )
                if text:
                    events.append(_claude_message_event(state, text))
            elif block_type == "thinking":
                thinking = _claude_stream_suffix(
                    state, block_type, _text(block.get("thinking"))
                )
                if thinking:
                    events.append({"kind": "thinking_delta", "payload": {"text": thinking}})
            elif block_type == "tool_use":
                tool_id = str(block.get("id") or "")[:200]
                if tool_id not in state.setdefault("tools", {}):
                    tool_name = str(block.get("name") or "tool")[:200]
                    _remember_tool(state, tool_id, tool_name)
                    events.append({
                        "kind": "tool_started",
                        "payload": {"id": tool_id, "name": tool_name},
                    })
        return events

    if record_type == "user":
        message = value.get("message")
        message = message if isinstance(message, dict) else {}
        content = message.get("content")
        content = content if isinstance(content, list) else []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_id = str(block.get("tool_use_id") or "")[:200]
            tool_name = state.setdefault("tools", {}).pop(tool_id, "tool")
            events.append({
                "kind": "tool_finished",
                "payload": {
                    "id": tool_id,
                    "name": tool_name,
                    "status": "error" if block.get("is_error") else "completed",
                    "content_bytes": _content_bytes(block.get("content")),
                },
            })
        return events

    if record_type == "result":
        if value.get("parent_tool_use_id") is not None:
            return []
        is_error_result = bool(value.get("is_error")) or subtype not in {"", "success"}
        result_text = value.get("result")
        if (
            not is_error_result
            and not state.get("result_fallback_emitted")
            and int(state.get("top_level_text_chars", 0)) == 0
            and isinstance(result_text, str)
            and result_text.strip()
        ):
            state["result_fallback_emitted"] = True
            events.append({
                "kind": "progress",
                "payload": {
                    "phase": "terminal_result_recovered",
                    "chars": len(result_text),
                },
            })
            events.append(_claude_message_event(state, result_text))
        elif (
            not is_error_result
            and isinstance(result_text, str)
            and int(state.get("top_level_text_chars", 0)) > 0
            and len(result_text) > (int(state["top_level_text_chars"]) * 2) + 512
        ):
            events.append({
                "kind": "warning",
                "payload": {
                    "message": "Claude result text exceeded emitted top-level text",
                    "subtype": "suspected_response_loss",
                    "result_chars": len(result_text),
                    "emitted_chars": int(state["top_level_text_chars"]),
                },
            })
        usage = value.get("usage")
        if isinstance(usage, dict):
            events.append({
                "kind": "usage",
                "payload": {
                    "scope": "total",
                    "usage": _bounded(usage),
                    "cost_usd": value.get("total_cost_usd"),
                    "num_turns": value.get("num_turns"),
                    "duration_ms": value.get("duration_ms"),
                    "stop_reason": str(value.get("stop_reason") or "")[:100],
                    "terminal_reason": subtype[:100],
                },
            })
        if is_error_result:
            events.append({
                "kind": "warning",
                "payload": {
                    "message": f"Claude result error: {subtype or 'unknown'}",
                    "subtype": subtype[:100],
                    "api_error_status": value.get("api_error_status"),
                },
            })
        denials = value.get("permission_denials")
        if isinstance(denials, list) and denials:
            names = []
            for denial in denials[:MAX_COLLECTION_ITEMS]:
                if isinstance(denial, dict):
                    names.append(str(denial.get("tool_name") or denial.get("name") or "tool")[:200])
                else:
                    names.append("tool")
            events.append({
                "kind": "warning",
                "payload": {
                    "message": "Claude denied one or more tool requests",
                    "tools": names,
                },
            })
        return events or [{"kind": "progress", "payload": {"phase": "result"}}]

    if record_type == "rate_limit_event":
        info = value.get("rate_limit_info")
        info = info if isinstance(info, dict) else value
        status = str(info.get("status") or "unknown")[:100]
        if status == "allowed":
            return []
        return [{
            "kind": "warning",
            "payload": {
                "message": f"Claude rate limit status: {status}",
                "rate_limit_type": str(info.get("rateLimitType") or "")[:100],
                "resets_at": info.get("resetsAt"),
            },
        }]

    return [{"kind": "progress", "payload": _claude_metadata(value)}]


def _kimi_metadata(value: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "role": str(value.get("role") or "")[:100],
        "type": str(value.get("type") or "")[:100],
    }
    return {key: item for key, item in payload.items() if item}


def _kimi_events(value: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    role = str(value.get("role") or "")
    record_type = str(value.get("type") or "")
    events: list[dict[str, Any]] = []

    if role == "assistant":
        content = _text(value.get("content"))
        if content:
            events.append({"kind": "message_delta", "payload": {"text": content}})
        tool_calls = value.get("tool_calls")
        if isinstance(tool_calls, list):
            for call in tool_calls[:MAX_COLLECTION_ITEMS]:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                function = function if isinstance(function, dict) else {}
                name = str(function.get("name") or "tool")[:200]
                tool_id = str(call.get("id") or name)[:200]
                _remember_tool(state, tool_id, name)
                events.append({
                    "kind": "progress",
                    "payload": {
                        "phase": "tool_requested",
                        "id": tool_id,
                        "name": name,
                        "argument_bytes": _content_bytes(function.get("arguments")),
                    },
                })
        return events or [{"kind": "progress", "payload": {"phase": "assistant"}}]

    if role == "tool":
        tool_id = str(value.get("tool_call_id") or "tool")[:200]
        name = state.setdefault("tools", {}).pop(tool_id, "tool")
        return [{
            "kind": "tool_finished",
            "payload": {
                "id": tool_id,
                "name": name,
                "status": "unknown",
                "content_bytes": _content_bytes(value.get("content")),
            },
        }]

    if role == "meta" and record_type == "system.version":
        return [{
            "kind": "progress",
            "payload": {
                "phase": "provider_version",
                "version": str(value.get("version") or "")[:100],
            },
        }]
    if role == "meta" and record_type == "session.resume_hint":
        return [{
            "kind": "progress",
            "payload": {
                "phase": "session_ready",
                "session_id": str(value.get("session_id") or "")[:200],
            },
        }]
    if role == "meta" and record_type == "turn.step.retrying":
        return [{
            "kind": "warning",
            "payload": {
                "message": _text(value.get("error_message"))[:500]
                or "Kimi is retrying a provider step",
                "subtype": record_type,
                "error_name": str(value.get("error_name") or "")[:100],
                "status_code": value.get("status_code"),
                "failed_attempt": value.get("failed_attempt"),
                "max_attempts": value.get("max_attempts"),
                "delay_ms": value.get("delay_ms"),
            },
        }]
    if record_type == "goal.summary":
        return [{
            "kind": "usage",
            "payload": {
                "scope": "goal",
                "status": str(value.get("status") or "")[:100],
                "turns": value.get("turnsUsed"),
                "tokens": value.get("tokensUsed"),
                "wall_clock_ms": value.get("wallClockMs"),
            },
        }]
    return [{"kind": "progress", "payload": _kimi_metadata(value)}]


def _coalesce_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    coalesced: list[dict[str, Any]] = []
    input_progress: dict[str, dict[str, Any]] = {}
    for event in events:
        kind = event.get("kind")
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        if kind in {"message_delta", "thinking_delta"} and coalesced:
            previous = coalesced[-1]
            previous_text = previous.get("payload", {}).get("text")
            text = payload.get("text")
            if previous.get("kind") == kind and isinstance(previous_text, str) and isinstance(text, str):
                if len((previous_text + text).encode("utf-8")) <= MAX_EVENT_TEXT_CHARS:
                    # Partial responses are ordered text, not block-addressable artifacts.
                    previous["payload"]["text"] = previous_text + text
                    continue
        if kind == "progress" and payload.get("phase") == "tool_input":
            tool_id = str(payload.get("tool_use_id") or "")
            existing = input_progress.get(tool_id)
            if existing is not None:
                existing["input_bytes"] = int(existing.get("input_bytes") or 0) + int(payload.get("input_bytes") or 0)
                continue
            input_progress[tool_id] = payload
        coalesced.append(event)
    return coalesced


class ProviderEventDecoder:
    """Incrementally decode complete provider JSONL records from raw bytes."""

    def __init__(self, provider: str):
        self.provider = provider
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buffer = ""
        self._provider_state: dict[str, Any] = {}

    def feed(self, data: bytes, *, final: bool = False) -> list[dict[str, Any]]:
        self._buffer += self._decoder.decode(data, final=final)
        lines = self._buffer.split("\n")
        self._buffer = "" if final else lines.pop()
        if final and lines and not lines[-1]:
            lines.pop()
        events: list[dict[str, Any]] = []
        for raw_line in lines:
            line = raw_line.rstrip("\r")
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                if self.provider in PRIVATE_STDOUT_PROVIDERS:
                    raw = line.encode("utf-8")
                    payload = {
                        "message": str(exc)[:500],
                        "raw_bytes": len(raw),
                        "raw_sha256": hashlib.sha256(raw).hexdigest(),
                    }
                else:
                    payload = {
                        "message": str(exc)[:500],
                        "raw": line[:MAX_EVENT_TEXT_CHARS],
                    }
                events.append({
                    "kind": "parse_error",
                    "payload": payload,
                })
                continue
            if not isinstance(value, dict):
                if self.provider in PRIVATE_STDOUT_PROVIDERS:
                    events.append({
                        "kind": "progress",
                        "payload": {"value_type": type(value).__name__},
                    })
                else:
                    events.append({
                        "kind": "provider_raw",
                        "payload": _raw_payload(value),
                    })
                continue
            if self.provider == "codex":
                events.extend(_codex_events(value))
            elif self.provider == "claude":
                events.extend(_claude_events(value, self._provider_state))
            elif self.provider == "kimi":
                events.extend(_kimi_events(value, self._provider_state))
            else:
                events.append({
                    "kind": "provider_raw",
                    "payload": _raw_payload(value),
                })
        return _coalesce_events(events)

    def finish(self) -> list[dict[str, Any]]:
        return self.feed(b"", final=True)
