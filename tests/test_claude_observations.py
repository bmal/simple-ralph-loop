"""Observation extraction from the Claude Backend's events, verified against a
recording sink without a terminal (the program's testing decisions, register G4/G5).

These drive the adapter's event accumulator directly and assert the facts it emits --
the orchestrator's tool use and context gauge, and the live subagent roster -- rather
than how any of them is later rendered. The Run console's rendering of the same value
types is proven separately in ``test_run_console``."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ralph.backends.claude import ClaudeEventResult, orchestrator_context
from ralph.console import (
    ContextObserved,
    KilledTask,
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


def _init(session: str = "s1", model: str = "claude-opus-4-8") -> dict:
    return {
        "type": "system",
        "subtype": "init",
        "session_id": session,
        "apiKeySource": "none",
        "model": model,
        "permissionMode": "bypassPermissions",
        "tools": ["Bash", "Read", "Edit"],
        "mcp_servers": [],
        "plugins": [],
    }


def _assistant(
    session: str,
    parts: list[dict],
    *,
    usage: dict | None = None,
    parent: str | None = None,
    model: str = "claude-opus-4-8",
) -> dict:
    message: dict = {"id": "m", "role": "assistant", "model": model, "content": parts}
    if usage is not None:
        message["usage"] = usage
    return {
        "type": "assistant",
        "session_id": session,
        "parent_tool_use_id": parent,
        "message": message,
    }


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _tool(name: str) -> dict:
    return {"type": "tool_use", "name": name, "id": "t", "input": {}}


def _background(session: str, tasks: list[dict]) -> dict:
    return {
        "type": "system",
        "subtype": "background_tasks_changed",
        "session_id": session,
        "tasks": tasks,
    }


class ClaudeObservationExtractionTest(unittest.TestCase):
    def _feed(self, events: list[dict]) -> RecordingSink:
        sink = RecordingSink()
        result = ClaudeEventResult("claude-opus-4-8", sink)
        for event in events:
            result.accept(event)
        return sink

    def test_orchestrator_tool_use_is_emitted_in_order(self) -> None:
        sink = self._feed(
            [
                _init(),
                _assistant("s1", [_tool("Bash"), _text("running"), _tool("Read")]),
            ]
        )
        tools = [o.name for o in sink.observations if isinstance(o, ToolObserved)]
        self.assertEqual(tools, ["Bash", "Read"])

    def test_orchestrator_context_sums_input_cache_read_and_cache_creation(self) -> None:
        sink = self._feed(
            [
                _init(),
                _assistant(
                    "s1",
                    [_text("thinking")],
                    usage={
                        "input_tokens": 100,
                        "cache_read_input_tokens": 200,
                        "cache_creation_input_tokens": 50,
                        "output_tokens": 9,
                    },
                ),
            ]
        )
        context = [o.tokens for o in sink.observations if isinstance(o, ContextObserved)]
        # input + cache-read + cache-creation, never the output tokens (register G5).
        self.assertEqual(context, [350])

    def test_an_event_without_usage_reports_no_context(self) -> None:
        sink = self._feed([_init(), _assistant("s1", [_text("no usage here")])])
        self.assertFalse(any(isinstance(o, ContextObserved) for o in sink.observations))

    def test_a_subagents_work_is_not_counted_in_the_orchestrator_gauge(self) -> None:
        # A subagent event (a non-null origin marker) is a separate window: its tool
        # use and its tokens never reach the orchestrator's gauge (register G5).
        sink = self._feed(
            [
                _init(),
                _assistant(
                    "s1",
                    [_tool("Grep"), _text("subagent working")],
                    usage={"input_tokens": 9999},
                    parent="toolu_1",
                ),
            ]
        )
        self.assertFalse(any(isinstance(o, ToolObserved) for o in sink.observations))
        self.assertFalse(any(isinstance(o, ContextObserved) for o in sink.observations))

    def test_the_live_subagent_roster_is_emitted_for_the_established_session(self) -> None:
        sink = self._feed(
            [
                _init(),
                _assistant("s1", [_text("launching helpers")]),
                _background(
                    "s1",
                    [
                        {"task_id": "t1", "description": "survey"},
                        {"task_id": "t2", "description": "review"},
                    ],
                ),
            ]
        )
        rosters = [o.roster for o in sink.observations if isinstance(o, SubagentsObserved)]
        self.assertEqual(rosters, [("survey", "review")])

    def test_a_foreign_session_background_change_names_no_subagents(self) -> None:
        # A crossed stream (a foreign session id) is not this Iteration's roster
        # (#49/#50 I6), so it emits no subagent Observation.
        sink = self._feed(
            [
                _init("s1"),
                _assistant("s1", [_text("work")]),
                _background("other-session", [{"task_id": "t1", "description": "survey"}]),
            ]
        )
        self.assertFalse(any(isinstance(o, SubagentsObserved) for o in sink.observations))

    def test_no_sink_is_a_no_op(self) -> None:
        # An accumulator with no sink still runs: the emits are silently dropped.
        result = ClaudeEventResult("claude-opus-4-8")
        result.accept(_init())
        result.accept(_assistant("s1", [_tool("Bash")]))  # must not raise

    def test_orchestrator_context_helper_ignores_non_integer_usage(self) -> None:
        # The arithmetic tolerates a missing or malformed field and reports None when
        # nothing usable is present, rather than asserting a zero gauge.
        self.assertIsNone(orchestrator_context(None))
        self.assertIsNone(orchestrator_context({}))
        self.assertIsNone(orchestrator_context({"input_tokens": True}))
        self.assertEqual(orchestrator_context({"input_tokens": 5}), 5)


class ClaudeKilledTaskObservationTest(unittest.TestCase):
    def test_the_killed_task_report_becomes_an_observation(self) -> None:
        from ralph.backends.claude import report_killed_tasks

        sink = RecordingSink()
        result = ClaudeEventResult("claude-opus-4-8", sink)
        result.killed_tasks = ["task_ci_verify", None]
        report_killed_tasks(result)
        killed = [o for o in sink.observations if isinstance(o, KilledTask)]
        self.assertEqual([o.task_id for o in killed], ["task_ci_verify", None])


if __name__ == "__main__":
    unittest.main()
