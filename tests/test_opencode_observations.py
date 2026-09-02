"""Observation extraction from the OpenCode Backend's events, verified against a
recording sink without a terminal (the program's testing decisions, register G4/G5).

These drive the adapter's event accumulator directly and assert the facts it emits --
the orchestrator's tool use, context gauge, and the Stage it declared -- rather than
how any of them is later rendered, and that a field OpenCode cannot supply (the
subagent roster) is emitted as nothing rather than a fabricated zero. The Run
console's rendering of the same value types is proven separately in
``test_run_console``; parity with the Claude adapter's extraction is proven in
``test_claude_observations``."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ralph.backends.opencode import EventResult, orchestrator_context
from ralph.console import (
    ContextObserved,
    Narrated,
    StageObserved,
    StepObserved,
    SubagentsObserved,
    ToolActivity,
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


def _text(text: str, part_id: str = "part_1", session: str = "ses_1") -> dict:
    return {
        "type": "text",
        "sessionID": session,
        "part": {
            "id": part_id,
            "sessionID": session,
            "messageID": "msg_1",
            "type": "text",
            "text": text,
        },
    }


def _delta(delta: str, part_id: str = "part_1", session: str = "ses_1") -> dict:
    # The other shape streamed text arrives in: an extension of a part already seen,
    # carrying only what is new rather than restating the whole part.
    return {
        "type": "message.part.delta",
        "properties": {"sessionID": session, "partID": part_id, "delta": delta},
    }


def _step_start(part_id: str = "prt_ss", session: str = "ses_1") -> dict:
    return {
        "type": "step_start",
        "sessionID": session,
        "part": {"id": part_id, "type": "step-start"},
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

    def test_streamed_text_is_reported_as_a_partial_passage_of_narration(self) -> None:
        # OpenCode streams a growing text part, so each report is the *addition* since
        # the last one and is marked partial: the console holds the line open rather
        # than breaking a sentence across rows (register G11).
        sink = self._feed([_text("Work "), _text("Work complete.")])
        narration = [o for o in sink.observations if isinstance(o, Narrated)]
        self.assertEqual([(o.text, o.partial) for o in narration], [("Work ", True), ("complete.", True)])
        # OpenCode runs no orchestrator/subagent split, so nothing is attributed away
        # from the Backend itself.
        self.assertEqual([o.subagent for o in narration], [None, None])

    def test_every_tool_state_update_is_reported_to_the_feed(self) -> None:
        # The feed shows the stream as it happened -- one marker per update -- while
        # the status line's ``ToolObserved`` count stays one per call (register G4).
        sink = self._feed(
            [
                _tool_use("bash", "prt_1", status="pending"),
                _tool_use("bash", "prt_1", status="completed"),
            ]
        )
        activity = [o for o in sink.observations if isinstance(o, ToolActivity)]
        self.assertEqual([(o.name, o.state) for o in activity], [("bash", "pending"), ("bash", "completed")])
        self.assertEqual(len([o for o in sink.observations if isinstance(o, ToolObserved)]), 1)

    def test_step_boundaries_are_reported_as_their_own_fact(self) -> None:
        sink = self._feed([_step_start(), _step_finish({"input": 1})])
        steps = [o.started for o in sink.observations if isinstance(o, StepObserved)]
        self.assertEqual(steps, [True, False])

    def test_a_declared_stage_is_reported_as_the_backend_speaks(self) -> None:
        # Read out of the stream while the Iteration runs rather than from the final
        # message alone, so the operator sees the stage as the session reaches it.
        sink = self._feed(
            [
                _text("<promise>STAGE: selecting a child</promise>"),
                _tool_use("bash", "prt_1"),
                _text(
                    "<promise>STAGE: selecting a child</promise>\n\nOn it.\n\n"
                    "<promise>STAGE: implementing</promise>",
                ),
            ]
        )
        stages = [o.label for o in sink.observations if isinstance(o, StageObserved)]
        self.assertEqual(stages, ["selecting a child", "implementing"])

    def test_a_growing_part_does_not_re_announce_the_stage_it_already_declared(
        self,
    ) -> None:
        # OpenCode restates the whole part on every update. Re-announcing an unchanged
        # declaration would keep restarting the staleness clock, so a stage the Backend
        # declared once and then abandoned would never give the field back to the last
        # tool (register G6).
        sink = self._feed(
            [
                _text("<promise>STAGE: implementing</promise>"),
                _text("<promise>STAGE: implementing</promise>\n\nEditing the loop."),
                _text("<promise>STAGE: implementing</promise>\n\nEditing the loop. Done."),
            ]
        )
        stages = [o.label for o in sink.observations if isinstance(o, StageObserved)]
        self.assertEqual(stages, ["implementing"])

    def test_a_stage_declared_by_a_streamed_delta_is_read_too(self) -> None:
        # A delta extends a part without restating it, so the declaration it completes
        # is only visible on that shape.
        sink = self._feed(
            [
                _text("Working.\n\n"),
                _delta("<promise>STAGE: "),
                _delta("finishing</promise>"),
            ]
        )
        stages = [o.label for o in sink.observations if isinstance(o, StageObserved)]
        self.assertEqual(stages, ["finishing"])

    def test_a_later_part_re_announces_the_stage_the_backend_re_entered(self) -> None:
        # The counterpart of the once-per-part rule: a declaration in a *new* part is a
        # fresh announcement even when it names the stage already showing, so a Backend
        # saying it is still there is heard and the staleness clock starts over.
        sink = self._feed(
            [
                _text("<promise>STAGE: implementing</promise>", part_id="p1"),
                _text("<promise>STAGE: implementing</promise>", part_id="p2"),
            ]
        )
        stages = [o.label for o in sink.observations if isinstance(o, StageObserved)]
        self.assertEqual(stages, ["implementing", "implementing"])

    def test_a_delta_on_text_never_attributed_to_the_backend_declares_nothing(self) -> None:
        # Text whose message was never established as the Backend's own is not narrated,
        # and it does not declare a stage either: an unattributed part must not put words
        # in the Backend's mouth on the status line.
        sink = self._feed(
            [
                {
                    "type": "message.part.updated",
                    "properties": {
                        "part": {
                            "id": "p9",
                            "sessionID": "ses_1",
                            "messageID": "msg_unknown",
                            "type": "text",
                            "text": "",
                        }
                    },
                },
                _delta("<promise>STAGE: impersonating</promise>", part_id="p9"),
            ]
        )
        self.assertEqual([o for o in sink.observations if isinstance(o, StageObserved)], [])

    def test_the_stage_label_is_whatever_wording_the_backend_chose(self) -> None:
        # Free text, not an enumeration Ralph imposes: the stages belong to the
        # operator's prompt, which Ralph snapshots but never reads.
        sink = self._feed([_text("<promise>STAGE: chasing the flake</promise>")])
        stages = [o.label for o in sink.observations if isinstance(o, StageObserved)]
        self.assertEqual(stages, ["chasing the flake"])

    def test_an_over_long_stage_label_is_bounded_and_a_malformed_one_refused(self) -> None:
        sink = self._feed(
            [
                _text("<promise>STAGE: " + "reticulating " * 8 + "</promise>", part_id="p1"),
                _text("<promise>STAGE:    </promise>", part_id="p2"),
                _text("<promise>STAGE: red \x1b[31mtext</promise>", part_id="p3"),
            ]
        )
        stages = [o.label for o in sink.observations if isinstance(o, StageObserved)]
        # Only the bounded one survives; neither malformed label is ever reported, so
        # nothing raw can reach the status line.
        self.assertEqual(len(stages), 1)
        self.assertLessEqual(len(stages[0]), 32)
        self.assertTrue(stages[0].startswith("reticulating"))
        self.assertTrue(stages[0].endswith("..."))

    def test_a_stage_marker_inside_code_or_quotation_declares_nothing(self) -> None:
        # The same visible-Markdown rule the outcome markers use: a stage quoted from
        # the prompt or shown in a code sample is not a declaration.
        quoted = "```\n<promise>STAGE: fenced</promise>\n```\n\n> <promise>STAGE: quoted</promise>"
        sink = self._feed([_text(quoted)])
        self.assertEqual([o for o in sink.observations if isinstance(o, StageObserved)], [])

    def test_tool_use_alone_never_declares_a_stage(self) -> None:
        # Stage is declared, never inferred from the tool mix (register G6).
        sink = self._feed([_tool_use("edit", "prt_1"), _step_finish({"input": 3})])
        self.assertEqual([o for o in sink.observations if isinstance(o, StageObserved)], [])

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
