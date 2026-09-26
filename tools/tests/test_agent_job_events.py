from __future__ import annotations

import json
import unittest

from pathlib import Path
import sys


TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

from agent_job_events import (  # noqa: E402
    MAX_CLAUDE_STREAM_BLOCKS,
    MAX_EVENT_RECORD_BYTES,
    MAX_EVENT_TEXT_CHARS,
    ProviderEventDecoder,
    bound_event_payload,
)


class ProviderEventDecoderTest(unittest.TestCase):
    @staticmethod
    def _jsonl(*values: dict[str, object]) -> bytes:
        return "".join(json.dumps(value) + "\n" for value in values).encode()

    def test_codex_json_split_across_chunks_yields_one_event(self) -> None:
        decoder = ProviderEventDecoder("codex")
        line = json.dumps({
            "type": "item.completed",
            "item": {"id": "message-1", "type": "agent_message", "text": "ready"},
        }).encode("utf-8") + b"\n"

        self.assertEqual([], decoder.feed(line[:17]))
        events = decoder.feed(line[17:])

        self.assertEqual(["message_delta"], [event["kind"] for event in events])
        self.assertEqual("ready", events[0]["payload"]["text"])

    def test_utf8_character_split_across_chunks_is_preserved(self) -> None:
        decoder = ProviderEventDecoder("codex")
        line = json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "café"},
        }, ensure_ascii=False).encode("utf-8") + b"\n"
        split = line.index("é".encode("utf-8")) + 1

        self.assertEqual([], decoder.feed(line[:split]))
        events = decoder.feed(line[split:])

        self.assertEqual("café", events[0]["payload"]["text"])

    def test_tool_boundaries_are_normalized_without_command_text(self) -> None:
        decoder = ProviderEventDecoder("codex")
        source = (
            json.dumps({
                "type": "item.started",
                "item": {"id": "tool-1", "type": "command_execution", "command": "secret"},
            })
            + "\n"
            + json.dumps({
                "type": "item.completed",
                "item": {"id": "tool-1", "type": "command_execution", "exit_code": 0},
            })
            + "\n"
        ).encode("utf-8")

        events = decoder.feed(source)

        self.assertEqual(["tool_started", "tool_finished"], [event["kind"] for event in events])
        self.assertNotIn("secret", json.dumps(events))

    def test_malformed_and_unknown_records_are_nonfatal(self) -> None:
        decoder = ProviderEventDecoder("codex")

        events = decoder.feed(b"not-json\n{\"type\":\"future.event\",\"value\":1}\n")

        self.assertEqual(["parse_error", "provider_raw"], [event["kind"] for event in events])

    def test_unknown_event_payload_has_a_total_size_bound(self) -> None:
        decoder = ProviderEventDecoder("codex")
        value = {
            "type": "future.event",
            "items": ["x" * MAX_EVENT_TEXT_CHARS for _ in range(50)],
        }

        events = decoder.feed((json.dumps(value) + "\n").encode())

        self.assertEqual("provider_raw", events[0]["kind"])
        payload = events[0]["payload"]
        self.assertTrue(payload["truncated"])
        self.assertLessEqual(
            len(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
            MAX_EVENT_TEXT_CHARS + 100,
        )

    def test_progress_payload_has_an_aggregate_record_bound(self) -> None:
        decoder = ProviderEventDecoder("codex")
        value = {
            "type": "item.completed",
            "item": {
                "type": "file_change",
                "changes": ["x" * MAX_EVENT_TEXT_CHARS for _ in range(50)],
            },
        }

        event = decoder.feed((json.dumps(value) + "\n").encode())[0]
        payload = bound_event_payload(event["payload"])

        self.assertTrue(payload["truncated"])
        self.assertLess(
            len(json.dumps({"payload": payload}, ensure_ascii=False).encode("utf-8")),
            MAX_EVENT_RECORD_BYTES,
        )

    def test_final_unterminated_line_is_decoded(self) -> None:
        decoder = ProviderEventDecoder("codex")
        decoder.feed(b'{"type":"turn.started"')
        events = decoder.feed(b"}", final=True)
        self.assertEqual("turn_started", events[0]["kind"])

    def test_claude_stream_deltas_are_not_duplicated_by_snapshots_or_result(self) -> None:
        decoder = ProviderEventDecoder("claude")
        events = decoder.feed(self._jsonl(
            {
                "type": "stream_event",
                "event": {"type": "message_start", "message": {"model": "claude-opus-5"}},
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": "hello "},
                },
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": "world"},
                },
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "hello world"}]},
            },
            {
                "type": "result", "subtype": "success", "is_error": False,
                "result": "hello world", "usage": {"output_tokens": 2},
            },
        ))

        text = "".join(
            event["payload"]["text"] for event in events if event["kind"] == "message_delta"
        )
        self.assertEqual("hello world", text)
        self.assertEqual(1, sum(event["kind"] == "usage" for event in events))

    def test_claude_tool_events_omit_inputs_results_and_signatures(self) -> None:
        decoder = ProviderEventDecoder("claude")
        events = decoder.feed(self._jsonl(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start", "index": 1,
                    "content_block": {
                        "type": "tool_use", "id": "tool-1", "name": "Read", "input": {},
                    },
                },
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta", "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": "secret-input"},
                },
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "signature_delta", "signature": "secret-signature"},
                },
            },
            {
                "type": "assistant",
                "message": {"content": [{
                    "type": "tool_use", "id": "tool-1", "name": "Read",
                    "input": {"file_path": "secret-path"},
                }]},
            },
            {
                "type": "user",
                "message": {"content": [{
                    "type": "tool_result", "tool_use_id": "tool-1",
                    "content": "secret-result",
                }]},
                "tool_use_result": {"content": "secret-outer-result"},
            },
        ))

        self.assertEqual(
            ["tool_started", "progress", "tool_finished"],
            [event["kind"] for event in events],
        )
        serialized = json.dumps(events)
        self.assertNotIn("secret", serialized)
        self.assertEqual(len("secret-input"), events[1]["payload"]["input_bytes"])
        self.assertEqual(len("secret-result"), events[-1]["payload"]["content_bytes"])
        self.assertEqual("Read", events[-1]["payload"]["name"])

    def test_claude_result_recovers_text_when_none_was_emitted(self) -> None:
        decoder = ProviderEventDecoder("claude")
        events = decoder.feed(self._jsonl({
            "type": "result", "subtype": "success", "is_error": False,
            "result": "fallback answer", "usage": {"output_tokens": 2},
        }))
        self.assertEqual(
            ["progress", "message_delta", "usage"],
            [event["kind"] for event in events],
        )
        self.assertEqual("terminal_result_recovered", events[0]["payload"]["phase"])
        self.assertEqual("fallback answer", events[1]["payload"]["text"])

    def test_claude_result_never_duplicates_streamed_text(self) -> None:
        decoder = ProviderEventDecoder("claude")
        events = decoder.feed(self._jsonl(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": "already emitted"},
                },
            },
            {
                "type": "result", "subtype": "success", "is_error": False,
                "result": "different fallback answer",
            },
        ))
        text = "".join(
            event["payload"]["text"] for event in events
            if event["kind"] == "message_delta"
        )
        self.assertEqual("already emitted", text)
        self.assertNotIn("different fallback answer", json.dumps(events))

    def test_claude_snapshot_collision_production_shape_recovers_answer(self) -> None:
        decoder = ProviderEventDecoder("claude")
        events = decoder.feed(self._jsonl(
            {
                "type": "stream_event",
                "event": {"type": "message_start", "message": {"id": "stream-id"}},
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start", "index": 0,
                    "content_block": {"type": "thinking"},
                },
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": ""},
                },
            },
            {
                "type": "assistant",
                "message": {"id": "snapshot-id", "content": [
                    {"type": "thinking", "thinking": ""},
                ]},
            },
            {
                "type": "assistant",
                "message": {"id": "snapshot-id", "content": [
                    {"type": "text", "text": "answer"},
                ]},
            },
            {
                "type": "result", "subtype": "success", "is_error": False,
                "result": "answer",
            },
        ))
        self.assertEqual(
            ["answer"],
            [event["payload"]["text"] for event in events if event["kind"] == "message_delta"],
        )
        self.assertFalse(any(
            event["kind"] == "progress"
            and event["payload"].get("phase") == "terminal_result_recovered"
            for event in events
        ))

    def test_claude_partially_streamed_block_emits_snapshot_suffix(self) -> None:
        decoder = ProviderEventDecoder("claude")
        events = decoder.feed(self._jsonl(
            {
                "type": "stream_event",
                "event": {"type": "message_start", "message": {"id": "msg"}},
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": "hel"},
                },
            },
            {
                "type": "assistant",
                "message": {"id": "msg", "content": [{"type": "text", "text": "hello"}]},
            },
        ))
        self.assertEqual(
            "hello",
            "".join(event["payload"]["text"] for event in events if event["kind"] == "message_delta"),
        )

    def test_claude_mismatched_snapshot_id_does_not_duplicate_stream(self) -> None:
        decoder = ProviderEventDecoder("claude")
        events = decoder.feed(self._jsonl(
            {
                "type": "stream_event",
                "event": {"type": "message_start", "message": {"id": "stream-id"}},
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": "hello world"},
                },
            },
            {
                "type": "assistant",
                "message": {"id": "snapshot-id", "content": [
                    {"type": "text", "text": "hello world"},
                ]},
            },
        ))
        self.assertEqual(
            "hello world",
            "".join(event["payload"]["text"] for event in events if event["kind"] == "message_delta"),
        )

    def test_claude_adjacent_duplicate_snapshot_emits_once(self) -> None:
        decoder = ProviderEventDecoder("claude")
        snapshot = {
            "type": "assistant",
            "message": {"id": "msg", "content": [{"type": "text", "text": "answer"}]},
        }
        events = decoder.feed(self._jsonl(snapshot, snapshot))
        self.assertEqual(
            ["answer"],
            [event["payload"]["text"] for event in events if event["kind"] == "message_delta"],
        )

    def test_claude_stream_block_tracking_overflow_prefers_no_duplication(self) -> None:
        decoder = ProviderEventDecoder("claude")
        records = [{
            "type": "stream_event",
            "event": {"type": "message_start", "message": {"id": "msg"}},
        }]
        records.extend({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta", "index": index,
                "delta": {"type": "text_delta", "text": "x"},
            },
        } for index in range(MAX_CLAUDE_STREAM_BLOCKS + 1))
        records.append({
            "type": "assistant",
            "message": {"id": "msg", "content": [{"type": "text", "text": "x"}]},
        })
        events = decoder.feed(self._jsonl(*records))
        self.assertEqual(
            MAX_CLAUDE_STREAM_BLOCKS + 1,
            sum(len(event["payload"]["text"]) for event in events if event["kind"] == "message_delta"),
        )

    def test_claude_error_nested_and_non_string_results_do_not_recover(self) -> None:
        values = (
            {"type": "result", "subtype": "error_max_turns", "result": "private"},
            {"type": "result", "subtype": "success", "is_error": True, "result": "private"},
            {"type": "result", "subtype": "future_success", "result": "private"},
            {
                "type": "result", "subtype": "success", "is_error": False,
                "parent_tool_use_id": "nested", "result": "private",
            },
            {"type": "result", "subtype": "success", "result": ["not", "text"]},
            {"type": "result", "subtype": "success", "result": "   "},
        )
        for value in values:
            with self.subTest(value=value):
                events = ProviderEventDecoder("claude").feed(self._jsonl(value))
                self.assertFalse(any(event["kind"] == "message_delta" for event in events))
                self.assertNotIn("private", json.dumps(events))

    def test_claude_duplicate_result_records_recover_once(self) -> None:
        decoder = ProviderEventDecoder("claude")
        record = {
            "type": "result", "subtype": "", "is_error": False,
            "result": "answer",
        }
        events = decoder.feed(self._jsonl(record, record))
        self.assertEqual(1, sum(event["kind"] == "message_delta" for event in events))
        self.assertEqual(1, sum(
            event["kind"] == "progress"
            and event["payload"].get("phase") == "terminal_result_recovered"
            for event in events
        ))

    def test_claude_suspected_mixed_loss_is_marked_partial(self) -> None:
        decoder = ProviderEventDecoder("claude")
        events = decoder.feed(self._jsonl(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": "short"},
                },
            },
            {
                "type": "result", "subtype": "success", "is_error": False,
                "result": "x" * 600,
            },
        ))
        warnings = [event for event in events if event["kind"] == "warning"]
        self.assertEqual("suspected_response_loss", warnings[0]["payload"]["subtype"])
        self.assertNotIn("x" * 600, json.dumps(events))

    def test_claude_nested_error_result_is_ignored_entirely(self) -> None:
        events = ProviderEventDecoder("claude").feed(self._jsonl({
            "type": "result", "parent_tool_use_id": "nested",
            "subtype": "error_max_turns", "is_error": True,
            "result": "private", "usage": {"output_tokens": 2},
        }))
        self.assertEqual([], events)

    def test_claude_waiting_uses_stream_thinking_not_token_estimates(self) -> None:
        decoder = ProviderEventDecoder("claude")
        events = decoder.feed(self._jsonl(
            {"type": "system", "subtype": "status", "status": "requesting"},
            {
                "type": "system", "subtype": "thinking_tokens",
                "estimated_tokens": 100, "estimated_tokens_delta": 50,
            },
            {
                "type": "stream_event", "parent_tool_use_id": None,
                "event": {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "working"},
                },
            },
        ))
        self.assertEqual(["waiting", "thinking_delta"], [event["kind"] for event in events])
        self.assertNotIn("estimated_tokens", json.dumps(events))

    def test_claude_assistant_snapshot_fills_only_unstreamed_block(self) -> None:
        decoder = ProviderEventDecoder("claude")
        events = decoder.feed(self._jsonl(
            {
                "type": "stream_event",
                "event": {"type": "message_start", "message": {"id": "msg-1"}},
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start", "index": 0,
                    "content_block": {"type": "thinking"},
                },
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start", "index": 1,
                    "content_block": {"type": "text"},
                },
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "already streamed"},
                },
            },
            {
                "type": "assistant",
                "message": {"id": "msg-1", "content": [{
                    "type": "thinking", "thinking": "already streamed",
                }]},
            },
            {
                "type": "assistant",
                "message": {"id": "msg-1", "content": [{"type": "text", "text": "recovered"}]},
            },
        ))
        self.assertEqual("recovered", events[-1]["payload"]["text"])
        self.assertEqual(1, sum(event["kind"] == "thinking_delta" for event in events))

    def test_claude_same_type_snapshot_recovers_later_unstreamed_block(self) -> None:
        decoder = ProviderEventDecoder("claude")
        events = decoder.feed(self._jsonl(
            {
                "type": "stream_event",
                "event": {"type": "message_start", "message": {"id": "msg-2"}},
            },
            *(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_start", "index": index,
                        "content_block": {"type": "text"},
                    },
                }
                for index in (0, 1)
            ),
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": "first"},
                },
            },
            {
                "type": "assistant",
                "message": {"id": "msg-2", "content": [{"type": "text", "text": "first"}]},
            },
            {
                "type": "assistant",
                "message": {"id": "msg-2", "content": [{"type": "text", "text": "second"}]},
            },
        ))
        text = "".join(
            event["payload"]["text"] for event in events if event["kind"] == "message_delta"
        )
        self.assertEqual("firstsecond", text)

    def test_claude_concurrent_tools_finish_out_of_order_with_names(self) -> None:
        decoder = ProviderEventDecoder("claude")
        events = decoder.feed(self._jsonl(
            *(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_start", "index": index,
                        "content_block": {"type": "tool_use", "id": tool_id, "name": name},
                    },
                }
                for index, tool_id, name in ((0, "a", "Glob"), (1, "b", "Read"))
            ),
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "tool_use_id": "b", "content": "x"}]},
            },
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "tool_use_id": "a", "content": "yy"}]},
            },
        ))
        finished = [event["payload"] for event in events if event["kind"] == "tool_finished"]
        self.assertEqual(["Read", "Glob"], [event["name"] for event in finished])
        self.assertEqual([1, 2], [event["content_bytes"] for event in finished])

    def test_claude_subagent_records_do_not_change_parent_state_or_emit(self) -> None:
        decoder = ProviderEventDecoder("claude")
        events = decoder.feed(self._jsonl(
            {
                "type": "stream_event",
                "event": {"type": "message_start", "message": {"id": "parent"}},
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": "parent"},
                },
            },
            {
                "type": "stream_event", "parent_tool_use_id": "parent-tool",
                "event": {"type": "message_start", "message": {"id": "child"}},
            },
            {
                "type": "stream_event", "parent_tool_use_id": "parent-tool",
                "event": {
                    "type": "content_block_start", "index": 0,
                    "content_block": {"type": "tool_use", "id": "sub-tool", "name": "PrivateTool"},
                },
            },
            {
                "type": "stream_event", "parent_tool_use_id": "parent-tool",
                "event": {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": "private subagent text"},
                },
            },
            {
                "type": "stream_event", "parent_tool_use_id": "parent-tool",
                "event": {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": "private-input"},
                },
            },
            {
                "type": "stream_event", "parent_tool_use_id": "parent-tool",
                "event": {
                    "type": "message_delta", "usage": {"output_tokens": 99},
                    "delta": {"stop_reason": "tool_use"},
                },
            },
            {
                "type": "assistant", "parent_tool_use_id": "parent-tool",
                "message": {"content": [{"type": "text", "text": "private snapshot"}]},
            },
            {
                "type": "user", "parent_tool_use_id": "parent-tool",
                "message": {"content": [{
                    "type": "tool_result", "tool_use_id": "sub-tool", "content": "private-result",
                }]},
            },
            {
                "type": "assistant",
                "message": {"id": "parent", "content": [{"type": "text", "text": "parent"}]},
            },
        ))
        self.assertEqual(["turn_started", "message_delta"], [event["kind"] for event in events])
        self.assertEqual("parent", events[-1]["payload"]["text"])
        self.assertNotIn("private", json.dumps(events))

    def test_claude_parse_error_hashes_raw_content(self) -> None:
        decoder = ProviderEventDecoder("claude")
        events = decoder.feed(b'{"tool_result":"secret-content"\n')
        self.assertEqual("parse_error", events[0]["kind"])
        self.assertNotIn("secret", json.dumps(events))
        self.assertGreater(events[0]["payload"]["raw_bytes"], 0)
        self.assertEqual(64, len(events[0]["payload"]["raw_sha256"]))

    def test_claude_input_deltas_coalesce_without_content(self) -> None:
        decoder = ProviderEventDecoder("claude")
        events = decoder.feed(self._jsonl(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start", "index": 2,
                    "content_block": {"type": "tool_use", "id": "tool-2", "name": "Write"},
                },
            },
            *(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta", "index": 2,
                        "delta": {"type": "input_json_delta", "partial_json": "secret"},
                    },
                }
                for _ in range(20)
            ),
        ))
        progress = [event for event in events if event["kind"] == "progress"]
        self.assertEqual(1, len(progress))
        self.assertEqual(120, progress[0]["payload"]["input_bytes"])
        self.assertNotIn("secret", json.dumps(events))

    def test_claude_error_result_preserves_reason_without_text(self) -> None:
        decoder = ProviderEventDecoder("claude")
        events = decoder.feed(self._jsonl({
            "type": "result", "subtype": "error_max_turns", "is_error": True,
            "usage": {"output_tokens": 10},
        }))
        self.assertEqual(["usage", "warning"], [event["kind"] for event in events])
        self.assertEqual("error_max_turns", events[-1]["payload"]["subtype"])
        self.assertTrue(all(event["kind"] != "message_delta" for event in events))

    def test_claude_permission_denials_keep_names_not_inputs(self) -> None:
        decoder = ProviderEventDecoder("claude")
        events = decoder.feed(self._jsonl({
            "type": "result", "subtype": "success", "is_error": False,
            "permission_denials": [{
                "tool_name": "Bash", "tool_input": {"command": "secret-command"},
            }],
        }))
        self.assertEqual("warning", events[0]["kind"])
        self.assertEqual(["Bash"], events[0]["payload"]["tools"])
        self.assertNotIn("secret", json.dumps(events))

    def test_claude_init_whitelists_counts_not_machine_inventory(self) -> None:
        decoder = ProviderEventDecoder("claude")
        events = decoder.feed(self._jsonl({
            "type": "system", "subtype": "init", "model": "opus",
            "tools": [{"name": "secret-tool"}], "skills": ["secret-skill"],
            "agents": ["secret-agent"], "mcp_servers": [{"name": "private"}],
        }))
        self.assertEqual(1, events[0]["payload"]["tool_count"])
        self.assertEqual(1, events[0]["payload"]["mcp_server_count"])
        self.assertNotIn("secret", json.dumps(events))

    def test_unknown_claude_record_retains_only_bounded_metadata(self) -> None:
        decoder = ProviderEventDecoder("claude")
        events = decoder.feed(self._jsonl({
            "type": "future_private_event", "subtype": "new",
            "content": "secret-content", "session_id": "secret-session",
        }))
        self.assertEqual("progress", events[0]["kind"])
        self.assertNotIn("secret", json.dumps(events))

    def test_opencode_malformed_unknown_and_non_dict_records_are_private(self) -> None:
        decoder = ProviderEventDecoder("opencode")
        events = decoder.feed(
            b'raw-secret\n'
            + self._jsonl(
                {"type": "future.event", "part": {"text": "secret-content"}},
            )
            + b'"secret-string"\n'
        )

        self.assertEqual(
            ["parse_error", "progress", "progress"],
            [event["kind"] for event in events],
        )
        self.assertNotIn("raw-secret", json.dumps(events))
        self.assertNotIn("secret-content", json.dumps(events))
        self.assertNotIn("secret-string", json.dumps(events))

    def test_opencode_utf8_and_json_split_across_chunks_are_preserved(self) -> None:
        decoder = ProviderEventDecoder("opencode")
        line = json.dumps(
            {"type": "text", "part": {"type": "text", "text": "café"}}, ensure_ascii=False
        ).encode("utf-8") + b"\n"
        split = line.index("é".encode("utf-8")) + 1

        self.assertEqual([], decoder.feed(line[:split]))
        events = decoder.feed(line[split:])

        self.assertEqual("café", events[0]["payload"]["text"])


    def test_opencode_events_keep_the_answer_and_drop_tool_content(self) -> None:
        decoder = ProviderEventDecoder("opencode")
        events = decoder.feed(self._jsonl(
            {"type": "step_start", "sessionID": "ses_1", "part": {"type": "step-start"}},
            {"type": "tool_use", "sessionID": "ses_1", "part": {
                "type": "tool", "callID": "call_1", "tool": "read",
                "state": {
                    "status": "completed", "input": {"filePath": "/repo/settings.py"},
                    "output": "API_TOKEN = 'do-not-journal'", "title": "settings.py",
                    "metadata": {}, "time": {"start": 1, "end": 2},
                },
            }},
            {"type": "text", "sessionID": "ses_1", "part": {
                "type": "text", "text": "No blocking findings.", "time": {"start": 2, "end": 3},
            }},
            {"type": "step_finish", "sessionID": "ses_1", "part": {
                "type": "step-finish", "reason": "stop", "cost": 0.0012,
                "tokens": {"input": 1200, "output": 80, "reasoning": 0, "cache": {"read": 400, "write": 0}},
            }},
            {"type": "error", "sessionID": "ses_1", "error": {"name": "APIError", "data": {
                "message": "Go usage limit reached", "statusCode": 429, "isRetryable": False,
            }}},
        )) + decoder.feed(b"not json API_TOKEN=abc\n", final=True)

        self.assertEqual(
            ["progress", "tool_finished", "message_delta", "usage", "warning", "parse_error"],
            [event["kind"] for event in events],
        )
        journal = json.dumps(events)
        self.assertNotIn("do-not-journal", journal)
        self.assertNotIn("settings.py", journal)
        self.assertNotIn("API_TOKEN=abc", journal)
        self.assertEqual({
            "id": "call_1", "name": "read", "status": "completed",
            "content_bytes": len("API_TOKEN = 'do-not-journal'"),
        }, events[1]["payload"])
        self.assertEqual("No blocking findings.", events[2]["payload"]["text"])
        self.assertEqual(1200, events[3]["payload"]["input_tokens"])
        self.assertEqual(400, events[3]["payload"]["cache_read_tokens"])
        self.assertEqual({
            "message": "Go usage limit reached", "subtype": "provider_error",
            "error_name": "APIError", "status_code": 429, "retryable": False,
        }, events[4]["payload"])

    def test_real_opencode_transcript_decodes_without_tool_content(self) -> None:
        # Captured from OpenCode 1.18.32 `run --format json` on the Go subscription.
        fixture = Path(__file__).with_name("fixtures") / "opencode-1.18.32-run.jsonl"
        raw = fixture.read_bytes()
        tool = next(
            json.loads(line)["part"]["state"]
            for line in raw.decode().splitlines()
            if json.loads(line)["type"] == "tool_use"
        )
        decoder = ProviderEventDecoder("opencode")
        events = decoder.feed(raw[:500]) + decoder.feed(raw[500:]) + decoder.finish()

        self.assertEqual(
            ["progress", "tool_finished", "usage", "progress", "message_delta", "usage"],
            [event["kind"] for event in events],
        )
        self.assertIn("a - b", events[4]["payload"]["text"])
        self.assertEqual("stop", events[5]["payload"]["reason"])
        journal = json.dumps(events)
        self.assertNotIn(tool["input"]["filePath"], journal)
        self.assertNotIn(tool["output"], journal)

if __name__ == "__main__":
    unittest.main()
