"""Claude multi-turn stream contract: a session may span several turns (a
background subagent that finishes after its launching turn forces a fresh init),
the stream is read to EOF, results are attributed positionally, every init
re-proves the Trust boundary, and completion is judged on the final turn. All
assertions are on external behaviour -- exit code, operator-facing stderr, and
the retained artifacts under .git/ralph/ -- never on the accumulator's internals."""

from __future__ import annotations

import json

from harness import RalphCliTestCase


class ClaudeMultiTurnTest(RalphCliTestCase):
    def _run(self, events: str, *extra: str):
        return self.run_ralph("--iterations", "1", *extra, backend="claude", env={"FAKE_CLAUDE_EVENTS": events})

    def _read_outcome(self):
        # A test may drive more than one run in the same worktree, so read the
        # newest run directory (names are timestamp-prefixed, so max is latest).
        runs = (self.repo / ".git" / "ralph" / "runs").iterdir()
        run_dir = max(runs, key=lambda path: path.name)
        return run_dir, json.loads((run_dir / "outcome.json").read_text())

    def _assert_per_turn_flush_handoff(self, result) -> None:
        # The one operator-visible shape every per-turn-flush stream must produce
        # (H6/I3), asserted identically wherever the continuation is proven: fail
        # closed, name the stream-shape change rather than the generic violation,
        # and still hand the session off as a consuming, resumable
        # backend_contract_failure with a working resume command.
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("a result per turn", result.stderr)
        self.assertNotIn("event after the terminal result", result.stderr)
        _, outcome = self._read_outcome()
        self.assertEqual(outcome["outcome"], "backend_contract_failure")
        self.assertIn("manual resume:", result.stderr)
        self.assertIn("--session claude-session-1", result.stderr)

    def test_two_and_three_turn_streams_complete_normally(self) -> None:
        # A background subagent that finishes after its launching turn opens a
        # second (and third) turn with a fresh init; Ralph reads to EOF, judges
        # the final turn, and finishes normally instead of ending the run.
        two_turn = self._claude_multiturn_events(
            [
                {"text": "Investigating with a helper."},
                {
                    "text": "Work complete.\n<promise>COMPLETE</promise>",
                    "subagents": ["Survey finished: no blockers found."],
                },
            ]
        )
        result = self._run(two_turn)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Work complete.", result.stdout)
        # Operator-facing stderr announces the run and stays clean of any halt.
        self.assertIn("ralph: backend claude", result.stderr)
        self.assertNotIn("RALPH NEEDS OPERATOR", result.stderr)
        self.assertNotIn("RALPH INCOMPLETE", result.stderr)
        run_dir, outcome = self._read_outcome()
        self.assertEqual(outcome["outcome"], "complete")
        self.assertEqual(outcome["session_id"], "claude-session-1")
        session = json.loads((run_dir / "session.json").read_text())
        self.assertEqual(session["session_id"], "claude-session-1")
        # The subagent's own work and the background registration stay in the
        # retained stream evidence even though neither is the Backend's answer.
        retained = (run_dir / "stdout.ndjson").read_text()
        self.assertIn("background_tasks_changed", retained)
        self.assertIn("Survey finished", retained)

        for path in self.calls.iterdir():
            path.unlink()
        three_turn = self._claude_multiturn_events(
            [
                {"text": "Turn one done."},
                {"text": "Turn two done."},
                {"text": "All children done.\n<promise>COMPLETE</promise>"},
            ]
        )
        result = self._run(three_turn)
        self.assertEqual(result.returncode, 0, result.stderr)
        _, outcome = self._read_outcome()
        self.assertEqual(outcome["outcome"], "complete")

    def test_a_teardown_tail_after_the_result_completes_normally(self) -> None:
        # On Claude Code 2.1.228 the CLI emits teardown events after the result
        # block whenever a session touched a background task -- even one that
        # drained cleanly and answered correctly. Both observed tails (a clean
        # drain and a park) must leave the Iteration judged on its final message
        # exactly as before, not fail the run and strand the budget.
        answer = "Work complete.\n<promise>COMPLETE</promise>"
        tails = {
            "clean drain": self._claude_clean_drain_teardown("claude-session-1"),
            "park": self._claude_park_teardown("claude-session-1"),
        }
        for signature, teardown in tails.items():
            with self.subTest(signature=signature):
                for path in self.calls.iterdir():
                    path.unlink()
                events = self._claude_multiturn_events([{"text": answer}], teardown=teardown)
                result = self._run(events)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Work complete.", result.stdout)
                self.assertNotIn("RALPH NEEDS OPERATOR", result.stderr)
                self.assertNotIn("event after the terminal result", result.stderr)
                _, outcome = self._read_outcome()
                self.assertEqual(outcome["outcome"], "complete")

    def test_a_parked_background_teardown_ending_in_a_reinit_completes_normally(self) -> None:
        # The #47 regression, caught by the H10 live smoke against Claude Code
        # 2.1.228: a session that launched a background task and ended its turn
        # while the task was still running gets a teardown tail that drains and
        # kills the task and then re-emits `system/init` as its final event. That
        # trailing init opens no turn -- nothing follows it -- so it is teardown,
        # not the per-turn-flush shape, and the Iteration must be judged on its
        # answer, not failed closed with the budget stranded. The synthesised
        # fixtures modelled a park tail ending in `task_summary` and missed this
        # trailing init; only the live run against the real CLI surfaced it.
        answer = "Launched in the background, not waiting.\n<promise>COMPLETE</promise>"
        events = [
            self._claude_init_event("claude-session-1"),
            self._claude_background_event("claude-session-1"),
            self._claude_assistant_event(answer, "claude-session-1"),
            self._claude_result_event(answer, "claude-session-1"),
            {
                "type": "system",
                "subtype": "background_tasks_changed",
                "session_id": "claude-session-1",
                "tasks": [],
            },
            {
                "type": "system",
                "subtype": "task_updated",
                "session_id": "claude-session-1",
                "task_id": "t1",
                "patch": {"status": "killed", "end_time": 1},
            },
            {
                "type": "system",
                "subtype": "task_notification",
                "session_id": "claude-session-1",
                "task_id": "t1",
                "status": "stopped",
            },
            self._claude_init_event("claude-session-1"),
        ]
        result = self._run(self._claude_stream(events))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Launched in the background, not waiting.", result.stdout)
        self.assertNotIn("RALPH NEEDS OPERATOR", result.stderr)
        self.assertNotIn("event after the terminal result", result.stderr)
        self.assertNotIn("a result per turn", result.stderr)
        _, outcome = self._read_outcome()
        self.assertEqual(outcome["outcome"], "complete")

    def test_a_valid_trailing_init_after_a_three_turn_stream_is_teardown(self) -> None:
        # I1/I2: the tolerated trailing shape still works at depth. A three-turn
        # background-subagent stream whose park teardown tail ends in a lone,
        # same-session, Trust-boundary-safe re-init opens no turn and leaves the
        # Iteration judged on its final message -- exactly as the one-turn park
        # reinit does, so validating the trailing init did not narrow the shape
        # the outage fix restored.
        teardown = self._claude_park_teardown("claude-session-1") + [
            self._claude_init_event("claude-session-1")
        ]
        events = self._claude_multiturn_events(
            [
                {"text": "Turn one done.", "subagents": ["Survey one finished."]},
                {"text": "Turn two done."},
                {"text": "All done.\n<promise>COMPLETE</promise>"},
            ],
            teardown=teardown,
        )
        result = self._run(events)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("All done.", result.stdout)
        self.assertNotIn("RALPH NEEDS OPERATOR", result.stderr)
        self.assertNotIn("a result per turn", result.stderr)
        self.assertNotIn("event after the terminal result", result.stderr)
        _, outcome = self._read_outcome()
        self.assertEqual(outcome["outcome"], "complete")

    def test_an_unrecognised_trailing_system_subtype_is_tolerated(self) -> None:
        # The rule is shape-based, not an allowlist (H4): a system subtype Ralph
        # has never seen, arriving after the result block, is teardown/telemetry
        # and is ignored, so a future CLI addition is not another outage.
        events = [
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event(
                "Done.\n<promise>COMPLETE</promise>", "claude-session-1"
            ),
            self._claude_result_event("Done.\n<promise>COMPLETE</promise>", "claude-session-1"),
            {"type": "system", "subtype": "post_turn_summary", "session_id": "claude-session-1"},
        ]
        result = self._run(self._claude_stream(events))
        self.assertEqual(result.returncode, 0, result.stderr)
        _, outcome = self._read_outcome()
        self.assertEqual(outcome["outcome"], "complete")

    def test_an_informational_event_between_two_results_is_tolerated(self) -> None:
        # The tolerance rule applies at every stream position, between results as
        # well as after them (H5), closing the previously separate and untested
        # inter-result regime: an informational event that arrives while results
        # are still outstanding no longer fails the run.
        events = [
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event("First pass.", "claude-session-1"),
            self._claude_background_event("claude-session-1"),
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event(
                "Done.\n<promise>COMPLETE</promise>", "claude-session-1"
            ),
            self._claude_result_event("First pass.", "claude-session-1"),
            {"type": "system", "subtype": "task_summary", "session_id": "claude-session-1"},
            self._claude_result_event("Done.\n<promise>COMPLETE</promise>", "claude-session-1"),
        ]
        result = self._run(self._claude_stream(events))
        self.assertEqual(result.returncode, 0, result.stderr)
        _, outcome = self._read_outcome()
        self.assertEqual(outcome["outcome"], "complete")

    def _bad_second_init(self, second_session: str = "claude-session-1", **override: object) -> str:
        events = [
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event("Turn one.", "claude-session-1"),
            self._claude_background_event("claude-session-1"),
            self._claude_init_event(second_session, **override),
            self._claude_assistant_event("Turn two.", "claude-session-1"),
            self._claude_result_event("Turn one.", "claude-session-1"),
            self._claude_result_event("Turn two.", "claude-session-1"),
        ]
        return self._claude_stream(events)

    def test_each_init_reproves_the_trust_boundary(self) -> None:
        # A later init that differs from the first in any isolation property, or in
        # the session id, fails the Iteration closed -- so a longer stream is a
        # stronger proof, not a weaker one. Each property is asserted separately.
        cases = [
            ({"apiKeySource": "ANTHROPIC_API_KEY"}, "did not use subscription OAuth"),
            ({"permissionMode": "default"}, "did not enter full-auto permission mode"),
            ({"mcp_servers": [{"name": "external"}]}, "external MCP servers or plugins"),
            ({"plugins": [{"name": "external"}]}, "external MCP servers or plugins"),
            ({"tools": ["Bash", "UnknownExternalTool"]}, "unknown or external tool"),
            ({"second_session": "claude-session-other"}, "inconsistent session metadata"),
        ]
        for override, message in cases:
            with self.subTest(override=override):
                for path in self.calls.iterdir():
                    path.unlink()
                result = self._run(self._bad_second_init(**override))
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(message, result.stderr)

    def test_a_post_result_init_reproves_the_trust_boundary_at_every_position(self) -> None:
        # Finding 1 (#50 I1/I2): a post-result init is tolerated as teardown, but
        # only after it re-proves the established session and the full Trust
        # boundary -- a crossed session id or any unsafe isolation metadata fails
        # closed, exactly as a turn-opening init does. The rule holds at both
        # result positions: after a complete result block and between two
        # outstanding results, so stream position never weakens validation.
        cases = [
            ({"apiKeySource": "ANTHROPIC_API_KEY"}, "did not use subscription OAuth"),
            ({"permissionMode": "default"}, "did not enter full-auto permission mode"),
            ({"mcp_servers": [{"name": "external"}]}, "external MCP servers or plugins"),
            ({"plugins": [{"name": "external"}]}, "external MCP servers or plugins"),
            ({"tools": ["Bash", "UnknownExternalTool"]}, "unknown or external tool"),
            ({"model": "claude-3-5-haiku"}, "did not match the selected model"),
            ({"session_id": "claude-session-other"}, "inconsistent session metadata"),
        ]

        def after_complete_block(hostile: dict) -> list[dict]:
            return [
                self._claude_init_event("claude-session-1"),
                self._claude_assistant_event("Turn one.", "claude-session-1"),
                self._claude_result_event("Turn one.", "claude-session-1"),
                hostile,
            ]

        def between_two_results(hostile: dict) -> list[dict]:
            return [
                self._claude_init_event("claude-session-1"),
                self._claude_assistant_event("Turn one.", "claude-session-1"),
                self._claude_background_event("claude-session-1"),
                self._claude_init_event("claude-session-1"),
                self._claude_assistant_event("Turn two.", "claude-session-1"),
                self._claude_result_event("Turn one.", "claude-session-1"),
                hostile,
                self._claude_result_event("Turn two.", "claude-session-1"),
            ]

        positions = {
            "after complete result block": after_complete_block,
            "between two outstanding results": between_two_results,
        }
        for position, build in positions.items():
            for override, message in cases:
                with self.subTest(position=position, override=override):
                    for path in self.calls.iterdir():
                        path.unlink()
                    hostile = self._claude_init_event("claude-session-1")
                    hostile.update(override)
                    result = self._run(self._claude_stream(build(hostile)))
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn(message, result.stderr)
                    # The leading init established the session id, so the refusal
                    # is a consuming, resumable handoff, not an outright failure.
                    self.assertIn("--session claude-session-1", result.stderr)
                    _, outcome = self._read_outcome()
                    self.assertEqual(outcome["outcome"], "backend_contract_failure")

    def test_duplicate_init_without_a_background_task_still_fails_closed(self) -> None:
        # The relaxation is licensed by observed cause: absent a
        # `background_tasks_changed`, a second init is the unexplained duplicate
        # it has always been.
        events = [
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event("Turn one.", "claude-session-1"),
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event("Turn two.", "claude-session-1"),
            self._claude_result_event("Turn one.", "claude-session-1"),
            self._claude_result_event("Turn two.", "claude-session-1"),
        ]
        result = self._run(self._claude_stream(events))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("duplicate initialization metadata", result.stderr)

    def test_a_registration_before_the_first_init_licenses_nothing(self) -> None:
        # F1 licenses the relaxation by *observed cause*: a background task some
        # turn launched. A registration arriving before any turn exists is not
        # one -- no turn could have made it -- so it must not license the later
        # duplicate init in a session that never launched anything.
        events = [
            self._claude_background_event("claude-session-1"),
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event("Turn one.", "claude-session-1"),
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event("Turn two.", "claude-session-1"),
            self._claude_result_event("Turn one.", "claude-session-1"),
            self._claude_result_event("Turn two.", "claude-session-1"),
        ]
        result = self._run(self._claude_stream(events))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("duplicate initialization metadata", result.stderr)
        _, outcome = self._read_outcome()
        self.assertEqual(outcome["outcome"], "backend_contract_failure")

    def test_a_registration_after_the_first_init_licenses_a_later_init_as_today(self) -> None:
        # The other side of the same edge: once a turn is open the registration
        # licenses exactly as before, including with a drained (empty) task list.
        drained = self._claude_background_event("claude-session-1")
        drained["tasks"] = []
        events = [
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event("Turn one.", "claude-session-1"),
            drained,
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event(
                "Done.\n<promise>COMPLETE</promise>", "claude-session-1"
            ),
            self._claude_result_event("Turn one.", "claude-session-1"),
            self._claude_result_event(
                "Done.\n<promise>COMPLETE</promise>", "claude-session-1"
            ),
        ]
        result = self._run(self._claude_stream(events))
        self.assertEqual(result.returncode, 0, result.stderr)
        _, outcome = self._read_outcome()
        self.assertEqual(outcome["outcome"], "complete")

    def test_a_result_count_that_does_not_match_the_turn_count_fails_closed(self) -> None:
        # Two turns but one result: the stream ended mid-session, so Ralph must not
        # judge a partial iteration.
        events = [
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event("Turn one.", "claude-session-1"),
            self._claude_background_event("claude-session-1"),
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event("Turn two.", "claude-session-1"),
            self._claude_result_event("Turn one.", "claude-session-1"),
        ]
        result = self._run(self._claude_stream(events))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("only some of its turns", result.stderr)

    def test_a_turn_with_only_subagent_output_and_no_backend_message_fails_closed(self) -> None:
        # A turn whose result is backed by no message of the Backend's own leaves
        # nothing to verify the positional attribution against, so fail closed
        # rather than trust an unbacked result.
        events = [
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event(
                "Subagent survey only.", "claude-session-1", parent_tool_use_id="toolu_only"
            ),
            self._claude_background_event("claude-session-1"),
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event("Turn two done.", "claude-session-1"),
            self._claude_result_event("Subagent survey only.", "claude-session-1"),
            self._claude_result_event("Turn two done.", "claude-session-1"),
        ]
        result = self._run(self._claude_stream(events))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("turn with no assistant response", result.stderr)

    def test_a_result_that_disagrees_with_its_turns_last_message_fails_closed(self) -> None:
        events = [
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event("The streamed answer.", "claude-session-1"),
            self._claude_result_event("A different answer.", "claude-session-1"),
        ]
        result = self._run(self._claude_stream(events))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("disagreed with the final assistant response", result.stderr)

    def test_subagent_messages_never_count_as_the_answer_or_a_marker(self) -> None:
        # A subagent speaks last and its message even carries a completion marker;
        # neither the response check nor marker detection may see it, so the run
        # is judged only on the Backend's own final message.
        events = [
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event("Still investigating.", "claude-session-1"),
            self._claude_assistant_event(
                "<promise>COMPLETE</promise>",
                "claude-session-1",
                parent_tool_use_id="toolu_sub_1",
            ),
            self._claude_result_event("Still investigating.", "claude-session-1"),
        ]
        result = self._run(self._claude_stream(events))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("iteration budget exhausted", result.stderr)
        _, outcome = self._read_outcome()
        self.assertEqual(outcome["outcome"], "budget_exhausted")

    def test_completion_is_judged_only_on_the_final_turn(self) -> None:
        # A completion claim an earlier turn made but later work superseded does
        # not complete the run.
        superseded = self._claude_multiturn_events(
            [
                {"text": "Done early.\n<promise>COMPLETE</promise>"},
                {"text": "Actually more remained; continuing."},
            ]
        )
        result = self._run(superseded)
        self.assertEqual(result.returncode, 1, result.stderr)
        _, outcome = self._read_outcome()
        self.assertEqual(outcome["outcome"], "budget_exhausted")

        for path in self.calls.iterdir():
            path.unlink()
        # The same marker in the final turn does complete the run.
        final = self._claude_multiturn_events(
            [
                {"text": "First pass."},
                {"text": "Everything landed.\n<promise>COMPLETE</promise>"},
            ]
        )
        result = self._run(final)
        self.assertEqual(result.returncode, 0, result.stderr)
        _, outcome = self._read_outcome()
        self.assertEqual(outcome["outcome"], "complete")

    def test_needs_input_in_the_final_turn_halts_with_a_resume_command(self) -> None:
        halting = self._claude_multiturn_events(
            [
                {"text": "Looking into it."},
                {"text": "<promise>NEEDS_INPUT</promise>\nWhich migration path should I take?"},
            ]
        )
        result = self._run(halting)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("RALPH NEEDS OPERATOR", result.stderr)
        self.assertIn("Which migration path", result.stderr)
        self.assertIn("ralph resume --backend claude", result.stderr)
        self.assertIn("--session claude-session-1", result.stderr)
        _, outcome = self._read_outcome()
        self.assertEqual(outcome["outcome"], "needs_input")

    def test_needs_input_withdrawn_by_the_final_turn_warns_and_continues(self) -> None:
        # A question an earlier turn raised but the final turn took back is not a
        # halt: warn on stderr and continue, mirroring the inferred-question split.
        withdrawn = self._claude_multiturn_events(
            [
                {"text": "<promise>NEEDS_INPUT</promise>\nShould I pick option A?"},
                {"text": "Resolved it from the tracker; option A it is."},
            ]
        )
        result = self._run(withdrawn)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn("RALPH NEEDS OPERATOR", result.stderr)
        self.assertIn("withdrew it", result.stderr)
        _, outcome = self._read_outcome()
        self.assertEqual(outcome["outcome"], "budget_exhausted")

    def test_needs_input_withdrawn_inside_the_final_turn_warns_and_continues(self) -> None:
        # The withdrawal F7 forgives is per *message*, not per turn: a marker the
        # Backend emitted in the final turn and then spoke past reaches neither
        # the halt nor the warning under a turn-granular scan, so a genuine
        # operator question is discarded silently. Built explicitly rather than
        # through _claude_multiturn_events because the shape is deliberately one
        # the well-formed builder cannot express: two Backend messages in a turn.
        events = [
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event("Turn one.", "claude-session-1"),
            self._claude_background_event("claude-session-1"),
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event(
                "<promise>NEEDS_INPUT</promise>\nWhich option, A or B?", "claude-session-1"
            ),
            self._claude_assistant_event(
                "Meanwhile I tidied the imports.", "claude-session-1"
            ),
            self._claude_result_event("Turn one.", "claude-session-1"),
            self._claude_result_event("Meanwhile I tidied the imports.", "claude-session-1"),
        ]
        result = self._run(self._claude_stream(events))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn("RALPH NEEDS OPERATOR", result.stderr)
        self.assertIn("withdrew it", result.stderr)
        self.assertIn("Which option, A or B?", result.stderr)
        _, outcome = self._read_outcome()
        self.assertEqual(outcome["outcome"], "budget_exhausted")

    def test_needs_input_withdrawn_inside_an_earlier_turn_warns_and_continues(self) -> None:
        # The same generalisation from the other side: the marker is in a
        # non-final message of a turn that is not the final one either, so no
        # result ever carries it.
        events = [
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event(
                "<promise>NEEDS_INPUT</promise>\nWhich option, A or B?", "claude-session-1"
            ),
            self._claude_assistant_event("Turn one, second thoughts.", "claude-session-1"),
            self._claude_background_event("claude-session-1"),
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event("Turn two done.", "claude-session-1"),
            self._claude_result_event("Turn one, second thoughts.", "claude-session-1"),
            self._claude_result_event("Turn two done.", "claude-session-1"),
        ]
        result = self._run(self._claude_stream(events))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn("RALPH NEEDS OPERATOR", result.stderr)
        self.assertIn("withdrew it", result.stderr)
        self.assertIn("Which option, A or B?", result.stderr)
        _, outcome = self._read_outcome()
        self.assertEqual(outcome["outcome"], "budget_exhausted")

    def test_the_withdrawn_marker_warning_does_not_blame_an_earlier_turn(self) -> None:
        # The scan is per message now, so the warning must not tell the operator
        # the marker came from an earlier *turn* -- it may have come from an
        # earlier message of the very turn the Iteration was judged on.
        withdrawn = self._claude_multiturn_events(
            [
                {"text": "<promise>NEEDS_INPUT</promise>\nShould I pick option A?"},
                {"text": "Resolved it from the tracker; option A it is."},
            ]
        )
        result = self._run(withdrawn)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("withdrew it", result.stderr)
        self.assertNotIn("earlier turn", result.stderr)

    def test_a_withdrawn_marker_warning_bounds_the_quoted_fragment(self) -> None:
        # The withdrawn-marker warning is a bounded interruption, not an outlet for
        # the backend's whole final message (#39): a long quoted question is capped,
        # so its far tail never reaches the operator's stderr even though the whole
        # of it stays in the retained stream.
        tail = "TAIL_SENTINEL_NOT_SHOWN"
        long_question = "Should I take path A, weighing " + ("considerations " * 40) + tail + "?"
        withdrawn = self._claude_multiturn_events(
            [
                {"text": f"<promise>NEEDS_INPUT</promise>\n{long_question}"},
                {"text": "Resolved it from the tracker; path A it is."},
            ]
        )
        result = self._run(withdrawn)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("withdrew it", result.stderr)
        # The head of the question is shown; the far tail past the cap is not.
        self.assertIn("Should I take path A", result.stderr)
        self.assertNotIn(tail, result.stderr)

    def test_completion_in_a_non_final_message_of_the_final_turn_does_not_complete(self) -> None:
        # The COMPLETE half is untouched by the needs-input generalisation: a
        # completion claim the Backend spoke past inside its final turn is still
        # superseded, exactly as one superseded across turns is.
        events = [
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event(
                "Done early.\n<promise>COMPLETE</promise>", "claude-session-1"
            ),
            self._claude_assistant_event("Actually more remained.", "claude-session-1"),
            self._claude_result_event("Actually more remained.", "claude-session-1"),
        ]
        result = self._run(self._claude_stream(events))
        self.assertEqual(result.returncode, 1, result.stderr)
        _, outcome = self._read_outcome()
        self.assertEqual(outcome["outcome"], "budget_exhausted")

    def test_a_session_that_continues_after_a_result_names_the_per_turn_flush(self) -> None:
        # F2's "results are flushed at EOF, in turn order" is the load-bearing
        # observation, taken from one Claude Code build. A build that closes each
        # turn with its own result must still fail closed -- but the operator who
        # meets this after an overnight run has to be pointed at the stream-shape
        # change, not at a misbehaving backend. The signature is a post-result
        # init that a turn actually *follows*: here the init is tolerated as
        # possible teardown, and it is the continuation assistant behind it --
        # closing a second turn Ralph cannot place -- that fails closed and names
        # the per-turn-flush cause (H6). A lone trailing init would be teardown and
        # would pass; the continuation is what proves the shape.
        events = [
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event("Turn one.", "claude-session-1"),
            self._claude_result_event("Turn one.", "claude-session-1"),
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event("Turn two.", "claude-session-1"),
            self._claude_result_event("Turn two.", "claude-session-1"),
        ]
        # Behaviour is unchanged: still fail closed, still a consuming resumable
        # handoff with a working resume command.
        self._assert_per_turn_flush_handoff(self._run(self._claude_stream(events)))

    def test_a_background_registration_does_not_buy_a_continuation_turn_a_pass(self) -> None:
        # Tolerating the teardown init is not a blank cheque, and a background
        # registration does not license a genuine second turn. A session that
        # registered a background task, flushed its first result, re-initialised,
        # and then went on to speak a continuation turn is the per-turn-flush shape
        # -- the follow-up assistant behind the post-result init fails closed with
        # the wording reserved for it, exactly as when no background task was ever
        # registered.
        events = [
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event("Turn one.", "claude-session-1"),
            self._claude_background_event("claude-session-1"),
            self._claude_result_event("Turn one.", "claude-session-1"),
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event("Turn two.", "claude-session-1"),
        ]
        self._assert_per_turn_flush_handoff(self._run(self._claude_stream(events)))

    def test_a_post_result_init_a_turn_follows_fails_closed_with_a_result_outstanding(
        self,
    ) -> None:
        # I3 holds at *every* result position (I2), not only once the result block
        # is complete. Here the second turn's result is still to come when a valid,
        # same-session, Trust-boundary-safe init arrives, and either follow-up
        # behind it closes a turn Ralph cannot place: an assistant extends a turn
        # that was already flushed, and the further result is no longer the
        # ordinary positional flush -- reading it as one let an unplaceable
        # continuation land on an already-spoken-for turn and passed the whole
        # stream as `complete`.
        answer = "Turn two.\n<promise>COMPLETE</promise>"
        follow_ups = {
            "further result": self._claude_result_event(answer, "claude-session-1"),
            "continuation assistant": self._claude_assistant_event(answer, "claude-session-1"),
        }
        for signature, follow_up in follow_ups.items():
            with self.subTest(signature=signature):
                for path in self.calls.iterdir():
                    path.unlink()
                events = [
                    self._claude_init_event("claude-session-1"),
                    self._claude_assistant_event("Turn one.", "claude-session-1"),
                    self._claude_background_event("claude-session-1"),
                    self._claude_init_event("claude-session-1"),
                    self._claude_assistant_event(answer, "claude-session-1"),
                    self._claude_result_event("Turn one.", "claude-session-1"),
                    self._claude_init_event("claude-session-1"),
                    follow_up,
                ]
                self._assert_per_turn_flush_handoff(self._run(self._claude_stream(events)))

    def test_other_events_after_the_terminal_result_keep_the_original_message(self) -> None:
        # Only a *continuing* session gets the new diagnostic; a second result or
        # a trailing assistant message is still an ordinary contract violation.
        trailing = {
            "second result": self._claude_result_event("Turn one.", "claude-session-1"),
            "trailing assistant": self._claude_assistant_event(
                "Turn one.", "claude-session-1"
            ),
        }
        for signature, event in trailing.items():
            with self.subTest(signature=signature):
                events = [
                    self._claude_init_event("claude-session-1"),
                    self._claude_assistant_event("Turn one.", "claude-session-1"),
                    self._claude_result_event("Turn one.", "claude-session-1"),
                    event,
                ]
                result = self._run(self._claude_stream(events))
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("event after the terminal result", result.stderr)
                self.assertNotIn("a result per turn", result.stderr)
                for path in self.calls.iterdir():
                    path.unlink()

    def test_model_usage_is_unioned_across_results(self) -> None:
        events = [
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event("First pass.", "claude-session-1"),
            self._claude_background_event("claude-session-1"),
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event(
                "Done.\n<promise>COMPLETE</promise>", "claude-session-1"
            ),
            self._claude_result_event(
                "First pass.",
                "claude-session-1",
                model_usage={"claude-opus-5": {"inputTokens": 1, "outputTokens": 1}},
            ),
            self._claude_result_event(
                "Done.\n<promise>COMPLETE</promise>",
                "claude-session-1",
                model_usage={"claude-3-5-haiku": {"inputTokens": 2, "outputTokens": 2}},
            ),
        ]
        result = self._run(self._claude_stream(events))
        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir, _ = self._read_outcome()
        session = json.loads((run_dir / "session.json").read_text())
        self.assertEqual(
            set(session["model_usage"]), {"claude-opus-5", "claude-3-5-haiku"}
        )

    def test_a_result_naming_a_non_claude_model_fails_closed(self) -> None:
        events = [
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event("Working.", "claude-session-1"),
            self._claude_result_event(
                "Working.",
                "claude-session-1",
                model_usage={"gpt-4o": {"inputTokens": 1, "outputTokens": 1}},
            ),
        ]
        result = self._run(self._claude_stream(events))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("valid model usage", result.stderr)

    def test_the_prompt_no_longer_carries_the_deleted_subagent_directive(self) -> None:
        # The parser now enforces what the directive asked for, so the tokens go.
        result = self.run_ralph(backend="claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        composed = json.loads((self.calls / "claude-stdin").read_text())["message"]["content"]
        self.assertNotIn("run_in_background", composed)
        self.assertNotIn("run synchronously", composed)
        # The Loop protocol is still appended unchanged.
        self.assertIn("at most one child issue", composed)

    def test_the_background_work_directive_is_appended_for_claude_only(self) -> None:
        # #48/H3 and #50 I5/finding 2: the Claude adapter tells the backend it may
        # launch background work but must not park a turn on it. It states both
        # observed outcomes of an unresolved task -- teardown may kill it, or it may
        # finish and reappear as a continuation Ralph cannot attribute -- and tells
        # the backend not to rely on a later delivery, rather than the disproved
        # categorical guarantee that the task is always killed and the notification
        # never arrives. Backgrounding stays supported. The directive is adapter-local:
        # OpenCode, with no background-task runtime, never carries it.
        result = self.run_ralph(backend="claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        claude_prompt = json.loads(
            (self.calls / "claude-stdin").read_text()
        )["message"]["content"]
        self.assertIn("must not end your turn while one is still unresolved", claude_prompt)
        self.assertIn("left unverified", claude_prompt)
        # Both observed outcomes are named, neither claimed to always happen.
        self.assertIn("tear down and kill it", claude_prompt)
        self.assertIn("continuation Ralph cannot attribute", claude_prompt)
        # The Backend is told not to bank on a later delivery.
        self.assertIn("do not stop expecting a later notification", claude_prompt)
        # The disproved categorical runtime guarantee is gone.
        self.assertNotIn("is never delivered", claude_prompt)
        self.assertNotIn("any task still in flight is killed", claude_prompt)
        self.assertNotIn("the moment you stop", claude_prompt)
        # Backgrounding itself stays supported -- the directive forbids abandoning.
        self.assertIn("launch background tasks where they help", claude_prompt)
        # It sits after the shared Loop protocol, which is still present unchanged.
        self.assertIn("at most one child issue", claude_prompt)
        for path in self.calls.iterdir():
            path.unlink()
        result = self.run_ralph(backend="opencode")
        self.assertEqual(result.returncode, 0, result.stderr)
        opencode_prompt = (self.calls / "stdin").read_text()
        self.assertNotIn("must not end your turn while", opencode_prompt)
        self.assertNotIn("left unverified", opencode_prompt)
        self.assertNotIn("still unresolved", opencode_prompt)
        self.assertNotIn("continuation Ralph cannot attribute", opencode_prompt)

    def test_a_killed_background_task_is_reported_by_name_without_changing_the_outcome(
        self,
    ) -> None:
        # H1/H7/H9: when the CLI's teardown explicitly reports it killed a
        # background task the backend left running (a `task_updated` marked
        # `killed`, never inferred from a task-list drain), Ralph names the
        # abandoned task on the stream the operator already watches for warnings --
        # and the Iteration keeps its ordinary outcome and final message, because
        # this is a report, not a verdict.
        answer = "Pushed to main; CI verifies in the background.\n<promise>COMPLETE</promise>"
        events = self._claude_multiturn_events(
            [{"text": answer}],
            teardown=self._claude_park_teardown("claude-session-1", task_id="task_ci_verify"),
        )
        result = self._run(events)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("killed a background task", result.stderr)
        # Named by the task id -- the identifier the observed killed event carries.
        self.assertIn("task_ci_verify", result.stderr)
        # The report neither reclassifies the Iteration nor touches its final message.
        self.assertIn("Pushed to main", result.stdout)
        self.assertNotIn("RALPH NEEDS OPERATOR", result.stderr)
        _, outcome = self._read_outcome()
        self.assertEqual(outcome["outcome"], "complete")

    def test_a_clean_drain_teardown_reports_no_killed_task(self) -> None:
        # H7: detection is the explicit killed-task report, never the task-list
        # drain. A session that drained its task cleanly mid-turn emits a drain and
        # a summary but no `killed` update, so Ralph must not fabricate an
        # abandonment warning for well-behaved work.
        answer = "Drained the helper and answered.\n<promise>COMPLETE</promise>"
        events = self._claude_multiturn_events(
            [{"text": answer}],
            teardown=self._claude_clean_drain_teardown("claude-session-1"),
        )
        result = self._run(events)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("killed a background task", result.stderr)
        _, outcome = self._read_outcome()
        self.assertEqual(outcome["outcome"], "complete")

    def _killed_update(self, session_id: str, task_id: str) -> dict:
        return {
            "type": "system",
            "subtype": "task_updated",
            "session_id": session_id,
            "task_id": task_id,
            "patch": {"status": "killed", "end_time": 1},
        }

    def test_a_killed_update_outside_the_established_session_names_no_task(self) -> None:
        # Finding 4 (#50 I6): a killed-task observation must belong to the
        # established session before it can reach the operator. A killed
        # `task_updated` whose session id is foreign, missing, or empty is a
        # crossed stream -- the same class #49 tightened for assistant and
        # licensing events -- so it fabricates no abandonment warning and cannot
        # be attributed to this Iteration, which stays complete on its own answer.
        answer = "Shipped; nothing left running.\n<promise>COMPLETE</promise>"
        crossed = self._killed_update("claude-session-1", "task_crossed")
        cases = {
            "foreign session": {**crossed, "session_id": "claude-session-other"},
            "empty session": {**crossed, "session_id": ""},
            "missing session": {key: value for key, value in crossed.items() if key != "session_id"},
        }
        for name, killed in cases.items():
            with self.subTest(case=name):
                for path in self.calls.iterdir():
                    path.unlink()
                teardown = [
                    {
                        "type": "system",
                        "subtype": "background_tasks_changed",
                        "session_id": "claude-session-1",
                        "tasks": [],
                    },
                    killed,
                ]
                result = self._run(
                    self._claude_multiturn_events([{"text": answer}], teardown=teardown)
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("killed a background task", result.stderr)
                self.assertNotIn("task_crossed", result.stderr)
                _, outcome = self._read_outcome()
                self.assertEqual(outcome["outcome"], "complete")

    def test_a_same_session_non_killed_task_update_names_no_task(self) -> None:
        # Finding 4 (#50 I6): detection is the explicit killed report alone. A
        # same-session `task_updated` whose patch does not mark the task `killed`
        # -- an ordinary status change -- is telemetry, not abandonment, so it
        # raises no warning and leaves the Iteration complete.
        answer = "Drained the helper and answered.\n<promise>COMPLETE</promise>"
        teardown = [
            {
                "type": "system",
                "subtype": "task_updated",
                "session_id": "claude-session-1",
                "task_id": "task_running",
                "patch": {"status": "completed", "end_time": 1},
            },
        ]
        result = self._run(self._claude_multiturn_events([{"text": answer}], teardown=teardown))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("killed a background task", result.stderr)
        self.assertNotIn("task_running", result.stderr)
        _, outcome = self._read_outcome()
        self.assertEqual(outcome["outcome"], "complete")

    def test_a_killed_task_warning_survives_a_later_contract_failure(self) -> None:
        # Finding 5 (#50 I7): once a same-session explicit kill is accepted, its
        # warning reaches the operator even when a later event fails the stream's
        # contract. A valid killed `task_updated` is followed here by an illegal
        # post-result assistant: the Iteration halts with a contract-failure
        # handoff, and the killed-task warning is emitted before that report -- the
        # halt does not hide the work the CLI said it abandoned. Reporting stays
        # observational: it does not replace or reclassify the failure (H1).
        answer = "Pushed to main; CI runs in the background.\n<promise>COMPLETE</promise>"
        events = [
            self._claude_init_event("claude-session-1"),
            self._claude_background_event("claude-session-1"),
            self._claude_assistant_event(answer, "claude-session-1"),
            self._claude_result_event(answer, "claude-session-1"),
            self._killed_update("claude-session-1", "task_ci_verify"),
            self._claude_assistant_event("Illegal continuation.", "claude-session-1"),
        ]
        result = self._run(self._claude_stream(events))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("killed a background task", result.stderr)
        self.assertIn("task_ci_verify", result.stderr)
        # The warning names the abandonment without reclassifying the halt.
        self.assertIn("event after the terminal result", result.stderr)
        _, outcome = self._read_outcome()
        self.assertEqual(outcome["outcome"], "backend_contract_failure")

    def test_a_killed_task_warning_survives_a_malformed_continuation(self) -> None:
        # Finding 5 (#50 I7): the delivery holds on a second failure path, proving
        # it is not tied to one handler. A valid killed `task_updated` is followed
        # by a line that is not valid JSON: the Iteration halts with a
        # contract-failure handoff, and the killed-task warning is still emitted
        # before that report.
        answer = "Pushed to main; CI runs in the background.\n<promise>COMPLETE</promise>"
        stream = self._claude_stream(
            [
                self._claude_init_event("claude-session-1"),
                self._claude_background_event("claude-session-1"),
                self._claude_assistant_event(answer, "claude-session-1"),
                self._claude_result_event(answer, "claude-session-1"),
                self._killed_update("claude-session-1", "task_ci_verify"),
            ]
        )
        result = self._run(stream + "\n{not valid json")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("killed a background task", result.stderr)
        self.assertIn("task_ci_verify", result.stderr)
        self.assertIn("malformed structured output", result.stderr)
        _, outcome = self._read_outcome()
        self.assertEqual(outcome["outcome"], "backend_contract_failure")

    def test_a_duplicate_killed_report_names_the_task_once(self) -> None:
        # Finding 4 (#50 I6): a repeated explicit killed report for a task already
        # recorded fabricates no additional abandoned task. The stream's evidence
        # is one killed task, so the operator is warned exactly once and no phantom
        # second task appears -- follow the evidence, never invent metadata.
        answer = "Done.\n<promise>COMPLETE</promise>"
        killed = self._killed_update("claude-session-1", "task_dup")
        teardown = [
            {
                "type": "system",
                "subtype": "background_tasks_changed",
                "session_id": "claude-session-1",
                "tasks": [],
            },
            killed,
            killed,
        ]
        result = self._run(self._claude_multiturn_events([{"text": answer}], teardown=teardown))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.count("killed a background task"), 1, result.stderr)
        self.assertIn("task_dup", result.stderr)
        _, outcome = self._read_outcome()
        self.assertEqual(outcome["outcome"], "complete")

    def test_an_assistant_event_that_omits_the_origin_marker_is_refused(self) -> None:
        # #49: the origin marker (`parent_tool_use_id`) is present on every
        # observed assistant event -- null on the Backend's own, an id on a
        # subagent's. An event that omits it entirely gives no origin to read, so
        # `.get()` would default it to the Backend and let it assemble the
        # Iteration's answer. It must be refused closed, not credited. The observed
        # valid shape (present and null) is exercised by every other test through
        # the fixture builders, so this asserts only the malformed omission.
        answer = "The real answer.\n<promise>COMPLETE</promise>"
        without_marker = self._claude_assistant_event(answer, "claude-session-1")
        del without_marker["parent_tool_use_id"]
        events = [
            self._claude_init_event("claude-session-1"),
            without_marker,
            self._claude_result_event(answer, "claude-session-1"),
        ]
        result = self._run(self._claude_stream(events))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("origin marker", result.stderr)
        # Session id was established by the init, so the refusal is a consuming,
        # resumable handoff rather than an outright failure.
        self.assertIn("--session claude-session-1", result.stderr)
        _, outcome = self._read_outcome()
        self.assertEqual(outcome["outcome"], "backend_contract_failure")

    def test_a_subagent_event_carrying_a_foreign_session_id_is_refused(self) -> None:
        # #49: every subagent assistant event observed carried its parent's
        # session id. One that carries a foreign id is a crossed stream and must be
        # refused, never allowed to contribute to the Iteration -- while a subagent
        # event carrying the parent's id (the observed valid shape) is admitted, as
        # test_subagent_messages_never_count_as_the_answer_or_a_marker exercises.
        events = [
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event("Working.", "claude-session-1"),
            self._claude_assistant_event(
                "Subagent from another session.",
                "claude-session-other",
                parent_tool_use_id="toolu_sub_1",
            ),
            self._claude_result_event("Working.", "claude-session-1"),
        ]
        result = self._run(self._claude_stream(events))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("inconsistent session metadata", result.stderr)
        _, outcome = self._read_outcome()
        self.assertEqual(outcome["outcome"], "backend_contract_failure")

    def test_a_registration_from_a_foreign_session_licenses_no_second_turn(self) -> None:
        # #49: the event that licenses a second init is honoured only when it
        # carries the session's own id. A `background_tasks_changed` bearing a
        # foreign id must not open the multi-turn relaxation, so the later init is
        # the unexplained duplicate it has always been -- exactly as when no
        # registration was seen at all. The same shape carrying the session's own
        # id licenses the second turn, as
        # test_a_registration_after_the_first_init_licenses_a_later_init_as_today
        # exercises.
        events = [
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event("Turn one.", "claude-session-1"),
            self._claude_background_event("claude-session-other"),
            self._claude_init_event("claude-session-1"),
            self._claude_assistant_event("Turn two.", "claude-session-1"),
            self._claude_result_event("Turn one.", "claude-session-1"),
            self._claude_result_event("Turn two.", "claude-session-1"),
        ]
        result = self._run(self._claude_stream(events))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("duplicate initialization metadata", result.stderr)
        _, outcome = self._read_outcome()
        self.assertEqual(outcome["outcome"], "backend_contract_failure")
