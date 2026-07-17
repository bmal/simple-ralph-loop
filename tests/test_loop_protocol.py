"""Loop protocol marker detection and handoff: completion/needs-input
markers, concluding-question heuristics, native questions, and
fail-closed stream parsing."""

from __future__ import annotations

import json
import shlex

from harness import RalphCliTestCase


class LoopProtocolTest(RalphCliTestCase):
    def test_opencode_invalid_utf8_streams_fail_closed_without_traceback(self) -> None:
        raw = self._invalid_utf8_file("bad-stdout.bin")
        stdout_result = self._run_guarded(
            env={"FAKE_EVENTS": self._events("Partial"), "FAKE_RAW_STDOUT_FILE": str(raw)}
        )
        self.assertEqual(stdout_result.returncode, 2, stdout_result.stderr)
        self.assertIn("invalid UTF-8", stdout_result.stderr)
        self.assertIn("--session ses_1", stdout_result.stderr)
        self.assertNotIn("Traceback", stdout_result.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        self.assertEqual(
            json.loads((run_dir / "outcome.json").read_text())["outcome"],
            "backend_contract_failure",
        )

        for path in self.calls.iterdir():
            path.unlink()
        stderr_result = self._run_guarded(env={"FAKE_RAW_STDERR_FILE": str(raw)})
        self.assertEqual(stderr_result.returncode, 2, stderr_result.stderr)
        self.assertIn("invalid UTF-8", stderr_result.stderr)
        self.assertNotIn("Traceback", stderr_result.stderr)

        for path in self.calls.iterdir():
            path.unlink()
        export_result = self._run_guarded(env={"FAKE_RAW_EXPORT_FILE": str(raw)})
        self.assertEqual(export_result.returncode, 2, export_result.stderr)
        self.assertIn("invalid UTF-8", export_result.stderr)
        self.assertNotIn("Traceback", export_result.stderr)

    def test_claude_invalid_utf8_streams_fail_closed_without_traceback(self) -> None:
        raw = self._invalid_utf8_file("bad-claude.bin")
        stdout_result = self._run_guarded(
            backend="claude",
            env={
                "FAKE_CLAUDE_EVENTS": self._claude_events("unused").splitlines()[0],
                "FAKE_CLAUDE_RAW_STDOUT_FILE": str(raw),
            },
        )
        self.assertEqual(stdout_result.returncode, 2, stdout_result.stderr)
        self.assertIn("invalid UTF-8", stdout_result.stderr)
        self.assertIn("--session claude-session-1", stdout_result.stderr)
        self.assertNotIn("Traceback", stdout_result.stderr)

        for path in self.calls.iterdir():
            path.unlink()
        stderr_result = self._run_guarded(
            backend="claude", env={"FAKE_CLAUDE_RAW_STDERR_FILE": str(raw)}
        )
        self.assertEqual(stderr_result.returncode, 2, stderr_result.stderr)
        self.assertIn("invalid UTF-8", stderr_result.stderr)
        self.assertNotIn("Traceback", stderr_result.stderr)

    def test_claude_partial_init_preserves_session_for_resumable_handoff(self) -> None:
        # A valid session id arrives in an init event whose other required fields
        # are malformed. The session must be checkpointed so the contract failure
        # becomes a consuming, resumable handoff.
        init = json.loads(self._claude_events("unused").splitlines()[0])
        del init["model"]
        result = self._run_guarded(
            backend="claude", env={"FAKE_CLAUDE_EVENTS": json.dumps(init)}
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("RALPH NEEDS OPERATOR", result.stderr)
        self.assertIn("ralph resume --backend claude", result.stderr)
        self.assertIn("--session claude-session-1", result.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        outcome = json.loads((run_dir / "outcome.json").read_text())
        self.assertEqual(outcome["outcome"], "backend_contract_failure")
        self.assertEqual(outcome["session_id"], "claude-session-1")
        session = json.loads((run_dir / "session.json").read_text())
        self.assertEqual(session["session_id"], "claude-session-1")
        self.assertFalse(session["final_result_received"])

    def test_explicitly_blocked_children_complete_but_ambiguous_blockers_do_not(self) -> None:
        blocked = "Every remaining child has declared open blockers.\n<promise>COMPLETE</promise>"
        blocked_result = self.run_ralph(
            env={"FAKE_EVENTS": self._events(blocked), "FAKE_EXPORT": self._export(blocked)}
        )
        self.assertEqual(blocked_result.returncode, 0, blocked_result.stderr)

        for path in self.calls.iterdir():
            path.unlink()
        ambiguous = "<promise>NEEDS_INPUT</promise>\nIs issue #9 actually a prerequisite?"
        ambiguous_result = self.run_ralph(
            env={"FAKE_EVENTS": self._events(ambiguous), "FAKE_EXPORT": self._export(ambiguous)}
        )
        self.assertNotEqual(ambiguous_result.returncode, 0)
        self.assertIn("RALPH NEEDS OPERATOR", ambiguous_result.stderr)
        self.assertIn("Is issue #9 actually a prerequisite?", ambiguous_result.stderr)
        self.assertIn("iterations remaining: 0", ambiguous_result.stderr)
        self.assertNotIn("continue Ralph:", ambiguous_result.stderr)

    def test_needs_input_wins_over_completion_and_prints_resume_commands(self) -> None:
        final = (
            "<promise>COMPLETE</promise>\n"
            "<promise>NEEDS_INPUT</promise>\n"
            "Should I preserve the legacy file?"
        )
        result = self.run_ralph(
            "--iterations",
            "3",
            env={"FAKE_EVENTS": self._events(final), "FAKE_EXPORT": self._export(final)},
        )

        self.assertEqual(result.returncode, 2)
        self.assertNotIn("\a", result.stderr)
        self.assertIn("RALPH NEEDS OPERATOR", result.stderr)
        self.assertIn("Should I preserve the legacy file?", result.stderr)
        self.assertIn("session: ses_1", result.stderr)
        self.assertIn("iterations remaining: 2", result.stderr)
        self.assertIn("ralph resume --backend opencode", result.stderr)
        self.assertIn("--session ses_1", result.stderr)
        self.assertIn("--model openai/gpt-5.6-sol", result.stderr)
        self.assertIn("--iterations 2", result.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        outcome = json.loads((run_dir / "outcome.json").read_text())
        self.assertEqual(outcome["outcome"], "needs_input")
        self.assertEqual(outcome["iterations"][0]["session_id"], "ses_1")

    def test_concluding_question_heuristic_ignores_non_prose_question_marks(self) -> None:
        ignored = (
            "Implemented the change.\n\n"
            "  > Should this quoted issue text block?\n"
            "```python\nvalue = choose(\"which?\")\n```\n"
            "Tool output: [request?status=ok]\n"
            "See https://example.invalid/search?q=ralph\n\n"
            "Verification passed."
        )
        ignored_result = self.run_ralph(
            env={"FAKE_EVENTS": self._events(ignored), "FAKE_EXPORT": self._export(ignored)}
        )
        self.assertEqual(ignored_result.returncode, 1)
        self.assertNotIn("NEEDS OPERATOR", ignored_result.stderr)

        for path in self.calls.iterdir():
            path.unlink()
        question = "Implementation is ready.\n\nShould I remove the compatibility shim?"
        question_result = self.run_ralph(
            env={"FAKE_EVENTS": self._events(question), "FAKE_EXPORT": self._export(question)}
        )
        self.assertEqual(question_result.returncode, 2)
        self.assertIn("Should I remove the compatibility shim?", question_result.stderr)

    def test_concluding_question_survives_trailing_closing_prose(self) -> None:
        # A genuine user-directed question is detected even when a courtesy
        # sign-off follows it, on one line or on a following line.
        cases = [
            "Implementation is staged.\n\nShould I proceed? Please advise.",
            "The migration is ready.\n\nShould I open the PR now?\nThanks!",
            "Work is done.\n\nWhich option should I use? Let me know when you can.",
        ]
        for backend in ("opencode", "claude"):
            for text in cases:
                with self.subTest(backend=backend, text=text):
                    result = self._run_backend_question(backend, text)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("RALPH NEEDS OPERATOR", result.stderr)
                    for path in self.calls.iterdir():
                        path.unlink()

    def test_quoted_titles_fences_urls_and_tool_logs_do_not_hand_off(self) -> None:
        # Quoted issue titles, nested code fences, URLs, and multi-line tool
        # logs all carry question marks but must never trigger a false handoff.
        ignored = (
            "Completed the work described in the parent issue.\n\n"
            "> Should the loop retry on failure?\n\n"
            "Resolved the ticket titled `Can we drop Python 3.10?` cleanly.\n\n"
            "````markdown\n```\nShould this nested fence trigger?\n```\n````\n\n"
            "[tool: bash]\n$ pytest -q\ncollected 5 items\nDid every case pass?\n.....\n\n"
            "Reference: https://example.invalid/issues?q=retry\n\n"
            "All acceptance criteria are satisfied."
        )
        for backend in ("opencode", "claude"):
            with self.subTest(backend=backend):
                result = self._run_backend_question(backend, ignored)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertNotIn("NEEDS OPERATOR", result.stderr)
                for path in self.calls.iterdir():
                    path.unlink()

    def test_claude_terminal_result_state_machine_fails_closed(self) -> None:
        base = self._claude_events("Implemented the change.").splitlines()
        init, assistant, terminal = base[0], base[1], base[2]

        contradictory = json.loads(terminal)
        contradictory["result"] = "A different final answer entirely."
        duplicated = "\n".join([init, assistant, terminal, terminal])
        after_result = "\n".join([init, assistant, terminal, assistant])
        result_before_init = "\n".join([terminal, init, assistant])

        cases = [
            ("\n".join([init, assistant, json.dumps(contradictory)]),
             "disagreed with the final assistant response"),
            (duplicated, "event after the terminal result"),
            (after_result, "event after the terminal result"),
            (result_before_init, "inconsistent session metadata"),
        ]
        for events, message in cases:
            with self.subTest(message=message):
                result = self.run_ralph(
                    backend="claude", env={"FAKE_CLAUDE_EVENTS": events}
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(message, result.stderr)
                for path in self.calls.iterdir():
                    path.unlink()

    def test_opencode_stream_rejects_inconsistent_metadata_but_ignores_unknown_events(self) -> None:
        second_session = json.loads(self._events("Later text", session_id="ses_other"))
        inconsistent = self._events("First text") + "\n" + json.dumps(second_session)
        inconsistent_result = self.run_ralph(
            env={"FAKE_EVENTS": inconsistent, "FAKE_EXPORT": self._export("First text")}
        )
        self.assertEqual(inconsistent_result.returncode, 2, inconsistent_result.stderr)
        self.assertIn("inconsistent session metadata", inconsistent_result.stderr)

        for path in self.calls.iterdir():
            path.unlink()
        forward = (
            json.dumps({"type": "server.heartbeat", "sessionID": "ses_1", "extra": {"n": 1}})
            + "\n"
            + self._events("Work complete.\n<promise>COMPLETE</promise>")
        )
        forward_result = self.run_ralph(
            env={
                "FAKE_EVENTS": forward,
                "FAKE_EXPORT": self._export("Work complete.\n<promise>COMPLETE</promise>"),
            }
        )
        self.assertEqual(forward_result.returncode, 0, forward_result.stderr)

    def test_opencode_native_question_stops_and_hands_off_immediately(self) -> None:
        question_event = {
            "type": "tool_use",
            "sessionID": "ses_question",
            "part": {
                "type": "tool",
                "tool": "question",
                "state": {"input": {"questions": [{"question": "Which format should I use?"}]}},
            },
        }
        result = self.run_ralph(
            env={"FAKE_EVENTS": json.dumps(question_event), "FAKE_EXPORT": self._export("unused")}
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("native question tool", result.stderr)
        self.assertIn("Which format should I use?", result.stderr)

    def test_started_backend_failure_hands_off_but_pre_session_failure_does_not(self) -> None:
        started = self.run_ralph(
            env={"FAKE_EVENTS": self._events("Partial work"), "FAKE_EXIT": "1"}
        )
        self.assertEqual(started.returncode, 2)
        self.assertIn("session failed", started.stderr)
        self.assertIn("--session ses_1", started.stderr)
        started_run = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        self.assertEqual(
            json.loads((started_run / "outcome.json").read_text())["outcome"],
            "backend_failure",
        )

        for path in self.calls.iterdir():
            path.unlink()
        not_started = self.run_ralph(
            env={"FAKE_EVENTS": json.dumps({"type": "status"}), "FAKE_EXIT": "1"}
        )
        self.assertEqual(not_started.returncode, 2)
        self.assertIn("OpenCode session failed", not_started.stderr)
        self.assertNotIn("RALPH NEEDS OPERATOR", not_started.stderr)

    def test_claude_native_question_hands_off_with_full_auto_resume(self) -> None:
        events = self._claude_events("unused").splitlines()
        assistant = json.loads(events[1])
        assistant["message"]["content"] = [
            {
                "type": "tool_use",
                "name": "AskUserQuestion",
                "input": {"questions": [{"question": "Which migration path should I take?"}]},
            }
        ]
        result = self.run_ralph(
            backend="claude",
            env={"FAKE_CLAUDE_EVENTS": "\n".join([events[0], json.dumps(assistant)])},
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Claude attempted a native question tool", result.stderr)
        self.assertIn("Which migration path should I take?", result.stderr)
        self.assertIn("ralph resume --backend claude", result.stderr)
        self.assertIn("--session claude-session-1", result.stderr)

    def test_claude_marker_prose_question_and_malformed_stream_handoff(self) -> None:
        marker = "<promise>NEEDS_INPUT</promise>\nShould Claude continue with option B?"
        marker_result = self.run_ralph(
            backend="claude",
            env={"FAKE_CLAUDE_EVENTS": self._claude_events(marker)},
        )
        self.assertEqual(marker_result.returncode, 2)
        self.assertIn("Should Claude continue with option B?", marker_result.stderr)

        for path in self.calls.iterdir():
            path.unlink()
        prose = "Changes are ready.\n\nWould you like me to delete the old file?"
        prose_result = self.run_ralph(
            backend="claude",
            env={"FAKE_CLAUDE_EVENTS": self._claude_events(prose)},
        )
        self.assertEqual(prose_result.returncode, 2)
        self.assertIn("Would you like me to delete the old file?", prose_result.stderr)

        for path in self.calls.iterdir():
            path.unlink()
        init = self._claude_events("unused").splitlines()[0]
        malformed_result = self.run_ralph(
            backend="claude",
            env={"FAKE_CLAUDE_EVENTS": init + "\nnot-json"},
        )
        self.assertEqual(malformed_result.returncode, 2)
        self.assertIn("Claude emitted malformed structured output", malformed_result.stderr)
        self.assertIn("--session claude-session-1", malformed_result.stderr)
        runs = sorted((self.repo / ".git" / "ralph" / "runs").iterdir())
        self.assertEqual(
            json.loads((runs[-1] / "outcome.json").read_text())["outcome"],
            "backend_contract_failure",
        )

    def test_needs_input_marker_must_be_an_exact_standalone_line(self) -> None:
        padded = " <promise>NEEDS_INPUT</promise> \nImplementation finished."
        result = self.run_ralph(
            env={"FAKE_EVENTS": self._events(padded), "FAKE_EXPORT": self._export(padded)}
        )

        self.assertEqual(result.returncode, 1)
        self.assertNotIn("RALPH NEEDS OPERATOR", result.stderr)

    def test_handoff_commands_shell_quote_prompt_and_worktree_paths(self) -> None:
        quoted_repo = self.base / "repo with ' quote"
        self.repo.rename(quoted_repo)
        self.repo = quoted_repo
        quoted_prompt = self.base / "prompt with ' quote.md"
        self.prompt.rename(quoted_prompt)
        self.prompt = quoted_prompt
        final = "<promise>NEEDS_INPUT</promise>\nWhich option should I use?"

        result = self.run_ralph(
            "--iterations",
            "2",
            env={"FAKE_EVENTS": self._events(final), "FAKE_EXPORT": self._export(final)},
        )

        self.assertEqual(result.returncode, 2)
        resume = next(
            line.removeprefix("manual resume: ")
            for line in result.stderr.splitlines()
            if line.startswith("manual resume: ")
        )
        restart = next(
            line.removeprefix("continue Ralph: ")
            for line in result.stderr.splitlines()
            if line.startswith("continue Ralph: ")
        )
        resume_cd, resume_args = resume.split(" && ", 1)
        restart_cd, restart_args = restart.split(" && ", 1)
        self.assertEqual(shlex.split(resume_cd), ["cd", str(self.repo.resolve())])
        self.assertEqual(shlex.split(restart_cd), ["cd", str(self.repo.resolve())])
        self.assertEqual(shlex.split(resume_args)[-2:], ["--session", "ses_1"])
        parsed_restart = shlex.split(restart_args)
        self.assertIn(str(self.prompt.resolve()), parsed_restart)
        self.assertIn(str(self.repo.resolve()), parsed_restart)

    def test_marker_in_tool_output_does_not_complete(self) -> None:
        tool = {
            "type": "message.part.updated",
            "properties": {
                "part": {
                    "sessionID": "ses_1",
                    "messageID": "msg_1",
                    "type": "tool",
                    "state": {"output": "<promise>COMPLETE</promise>"},
                }
            },
        }
        result = self.run_ralph(
            env={
                "FAKE_EVENTS": json.dumps(tool) + "\n" + self._events("Not complete yet."),
                "FAKE_EXPORT": self._export("Not complete yet."),
            }
        )

        self.assertNotEqual(result.returncode, 0)

    def test_marker_in_code_or_quotation_does_not_complete(self) -> None:
        final = (
            "Quoted marker:\n> <promise>COMPLETE</promise>\n"
            "````text\n```~\n```\n<promise>COMPLETE</promise>\n````\n"
            "~~~`example`\n<promise>COMPLETE</promise>\n~~~"
        )
        result = self.run_ralph(
            env={"FAKE_EVENTS": self._events(final), "FAKE_EXPORT": self._export(final)}
        )

        self.assertNotEqual(result.returncode, 0)

    def test_tool_and_step_progress_is_readable(self) -> None:
        progress = [
            {"type": "step_start", "sessionID": "ses_1", "part": {"type": "step-start"}},
            {
                "type": "tool_use",
                "sessionID": "ses_1",
                "part": {"type": "tool", "tool": "bash", "state": {"status": "completed"}},
            },
            {"type": "step_finish", "sessionID": "ses_1", "part": {"type": "step-finish"}},
        ]
        events = "\n".join(json.dumps(item) for item in progress) + "\n" + self._events("Finished")
        result = self.run_ralph(
            env={"FAKE_EVENTS": events, "FAKE_EXPORT": self._export("Finished")}
        )

        self.assertIn("[step started]", result.stdout)
        self.assertIn("[bash (completed)]", result.stdout)
        self.assertIn("[step finished]", result.stdout)
        self.assertIn("full-auto mode", result.stderr)

    def test_deeply_nested_json_fails_closed_without_traceback(self) -> None:
        # JSON nested past the interpreter's recursion limit raises RecursionError
        # rather than json.JSONDecodeError. Both backends must treat it as
        # malformed structured output and hand off, never emit a raw traceback.
        # CPython 3.13 guards C recursion by probing real stack headroom, so a
        # fixed shallow depth parses fine when the process has a roomy stack.
        # macOS hard-caps thread stacks at 64 MiB, which 5M frames always
        # exceed, so this depth deterministically raises RecursionError.
        deep = self.base / "deep.json"
        depth = 5_000_000
        deep.write_text("[" * depth + "]" * depth + "\n", encoding="utf-8")

        opencode = self._run_guarded(
            env={
                "FAKE_EVENTS": self._events("Partial work"),
                "FAKE_RAW_STDOUT_FILE": str(deep),
            }
        )
        self.assertEqual(opencode.returncode, 2, opencode.stderr)
        self.assertIn("malformed structured output", opencode.stderr)
        self.assertIn("--session ses_1", opencode.stderr)
        self.assertNotIn("Traceback", opencode.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        self.assertEqual(
            json.loads((run_dir / "outcome.json").read_text())["outcome"],
            "backend_contract_failure",
        )

        for path in self.calls.iterdir():
            path.unlink()
        claude = self._run_guarded(
            backend="claude",
            env={
                "FAKE_CLAUDE_EVENTS": self._claude_events("unused").splitlines()[0],
                "FAKE_CLAUDE_RAW_STDOUT_FILE": str(deep),
            },
        )
        self.assertEqual(claude.returncode, 2, claude.stderr)
        self.assertIn("malformed structured output", claude.stderr)
        self.assertIn("--session claude-session-1", claude.stderr)
        self.assertNotIn("Traceback", claude.stderr)
