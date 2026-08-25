"""Observation extraction from the OpenCode Backend's events, verified against a
recording sink without a terminal (the program's testing decisions, register G4/G5).

These drive the adapter's event accumulator directly and assert the facts it emits --
the orchestrator's tool use and context gauge -- rather than how any of them is later
rendered, and that a field OpenCode cannot supply (the subagent roster) is emitted as
nothing rather than a fabricated zero. The Run console's rendering of the same value
types is proven separately in ``test_run_console``; parity with the Claude adapter's
extraction is proven in ``test_claude_observations``."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ralph.backends.opencode import EventResult, orchestrator_context
from ralph.console import (
    ContextObserved,
    SubagentsObserved,
    ToolObserved,
)


class RecordingSink:
    """A minimal ``ObservationSink``: it records every Observation so a test can
    assert on the facts the adapter emitted, with no terminal in sight."""

    def __init__(self) -> None:
        self.observations: list[object] = []

    def observe(self, observation: object) -> None:
        self.observations.append(observation)


def _tool_use(tool: str, part_id: str, status: str = "completed", session: str = "ses_1") -> dict:
    # The direct ``run --format json`` shape: a top-level ``tool_use`` event whose
    # ``part`` is the tool part, streamed under one id across its state updates.
    return {
        "type": "tool_use",
        "sessionID": session,
        "part": {
            "id": part_id,
            "sessionID": session,
            "messageID": "msg_1",
            "type": "tool",
            "callID": "call_" + part_id,
            "tool": tool,
            "state": {"status": status},
        },
    }


def _step_finish(tokens: dict | None, part_id: str = "prt_step", session: str = "ses_1") -> dict:
    part: dict = {"id": part_id, "type": "step-finish", "reason": "stop"}
    if tokens is not None:
        part["tokens"] = tokens
    return {"type": "step_finish", "sessionID": session, "part": part}


def _wrapped_tool(tool: str, part_id: str, status: str = "running", session: str = "ses_1") -> dict:
    # The alternate bus shape the adapter also tolerates: a ``message.part.updated``
    # event carrying the tool part under ``properties.part``.
    return {
        "type": "message.part.updated",
        "properties": {
            "part": {
                "id": part_id,
                "sessionID": session,
                "type": "tool",
                "tool": tool,
                "state": {"status": status},
            }
        },
    }


def _wrapped_step_finish(tokens: dict, part_id: str = "prt_ws", session: str = "ses_1") -> dict:
    return {
        "type": "message.part.updated",
        "properties": {
            "part": {"id": part_id, "sessionID": session, "type": "step-finish", "tokens": tokens}
        },
    }


class OpenCodeObservationExtractionTest(unittest.TestCase):
    def _feed(self, events: list[dict]) -> RecordingSink:
        sink = RecordingSink()
        result = EventResult("openai/gpt-5.6-sol", sink)
        for event in events:
            result.accept(event)
        return sink

    def test_orchestrator_tool_use_is_emitted_in_order(self) -> None:
        sink = self._feed(
            [
                _tool_use("bash", "prt_1"),
                _tool_use("read", "prt_2"),
            ]
        )
        tools = [o.name for o in sink.observations if isinstance(o, ToolObserved)]
        self.assertEqual(tools, ["bash", "read"])

    def test_a_tool_part_is_counted_once_across_its_state_updates(self) -> None:
        # A tool part streams several updates (pending -> running -> completed) under
        # one id; the status line's tool count must not triple as a result.
        sink = self._feed(
            [
                _tool_use("bash", "prt_1", status="pending"),
                _tool_use("bash", "prt_1", status="running"),
                _tool_use("bash", "prt_1", status="completed"),
            ]
        )
        tools = [o.name for o in sink.observations if isinstance(o, ToolObserved)]
        self.assertEqual(tools, ["bash"])

    def test_orchestrator_context_is_input_plus_cache_read_not_the_total(self) -> None:
        sink = self._feed(
            [
                _step_finish(
                    {"input": 671, "output": 8, "reasoning": 0, "cache": {"read": 21415, "write": 0}}
                )
            ]
        )
        context = [o.tokens for o in sink.observations if isinstance(o, ContextObserved)]
        # input + cache-read only (register G5): never the output, reasoning, or
        # cache-write tokens, and never OpenCode's own folded-in total.
        self.assertEqual(context, [671 + 21415])

    def test_a_step_finish_without_usable_tokens_reports_no_context(self) -> None:
        sink = self._feed([_step_finish(None), _step_finish({"output": 9})])
        self.assertFalse(any(isinstance(o, ContextObserved) for o in sink.observations))

    def test_the_wrapped_bus_shape_reports_the_same_tool_and_context(self) -> None:
        # The adapter tolerates the alternate ``message.part.updated`` shape; the
        # Observations it yields must match the direct shape's.
        sink = self._feed(
            [
                _wrapped_tool("grep", "prt_w", status="pending"),
                _wrapped_tool("grep", "prt_w", status="completed"),
                _wrapped_step_finish({"input": 10, "cache": {"read": 5}}),
            ]
        )
        tools = [o.name for o in sink.observations if isinstance(o, ToolObserved)]
        context = [o.tokens for o in sink.observations if isinstance(o, ContextObserved)]
        self.assertEqual(tools, ["grep"])
        self.assertEqual(context, [15])

    def test_no_subagent_roster_is_ever_emitted(self) -> None:
        # OpenCode has no subagent stream at all, so the adapter emits nothing for it
        # and the Run console renders the count as absent, not zero (register G5).
        sink = self._feed(
            [
                _tool_use("bash", "prt_1"),
                _step_finish({"input": 5, "cache": {"read": 1}}),
            ]
        )
        self.assertFalse(any(isinstance(o, SubagentsObserved) for o in sink.observations))

    def test_no_sink_is_a_no_op(self) -> None:
        # An accumulator with no sink still runs: the emits are silently dropped.
        result = EventResult("openai/gpt-5.6-sol")
        result.accept(_tool_use("bash", "prt_1"))  # must not raise
        result.accept(_step_finish({"input": 1, "cache": {"read": 1}}))  # must not raise

    def test_orchestrator_context_helper_ignores_malformed_tokens(self) -> None:
        # The arithmetic tolerates a missing or malformed field and reports None when
        # nothing usable is present, rather than asserting a zero gauge.
        self.assertIsNone(orchestrator_context(None))
        self.assertIsNone(orchestrator_context({}))
        self.assertIsNone(orchestrator_context({"input": True}))
        self.assertIsNone(orchestrator_context({"cache": {"read": True}}))
        self.assertEqual(orchestrator_context({"input": 5}), 5)
        self.assertEqual(orchestrator_context({"cache": {"read": 7}}), 7)
        self.assertEqual(orchestrator_context({"input": 5, "cache": {"read": 7}}), 12)
        # A cache object with no read tokens but a present input still reports input.
        self.assertEqual(orchestrator_context({"input": 5, "cache": {"write": 3}}), 5)


if __name__ == "__main__":
    unittest.main()
