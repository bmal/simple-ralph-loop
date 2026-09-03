"""Run loop and iteration budget: fresh-session cadence, budget bounds,
backend/model announcement, per-iteration trust re-proof, branch reporting, and
how an Iteration's outcome is named and closed on every terminal path."""

from __future__ import annotations

import json
import signal

from harness import RalphCliTestCase


class RunLoopTest(RalphCliTestCase):
    def test_exact_completion_runs_safely_and_retains_evidence(self) -> None:
        # An ambient opt-out must not disable OpenCode's built-in OAuth plugin in
        # the isolated child environment.
        result = self.run_ralph(env={"OPENCODE_DISABLE_DEFAULT_PLUGINS": "true"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Work complete.", result.stderr)
        run_dirs = list((self.repo / ".git" / "ralph" / "runs").iterdir())
        self.assertEqual(len(run_dirs), 1)
        run_dir = run_dirs[0]
        self.assertEqual((run_dir / "prompt.txt").read_text(), "Implement the selected issue.\n")
        self.assertEqual(json.loads((run_dir / "outcome.json").read_text())["outcome"], "complete")
        self.assertEqual(json.loads((run_dir / "outcome.json").read_text())["session_id"], "ses_1")
        self.assertIn("backend diagnostic", (run_dir / "stderr.log").read_text())
        composed = (self.calls / "stdin").read_text()
        self.assertIn("Implement the selected issue.", composed)
        self.assertIn("at most one child issue", composed)
        self.assertIn("<promise>COMPLETE</promise>", composed)
        self.assertIn("explicit completion conditions", composed)
        invocation = (self.calls / "opencode").read_text()
        self.assertIn("run --model openai/gpt-5.6-sol --format json --auto", invocation)
        self.assertIn("-im", (self.calls / "caffeinate").read_text())
        child_env = (self.calls / "env").read_text()
        self.assertIn("OPENCODE_DISABLE_AUTOUPDATE=true", child_env)
        self.assertNotIn("OPENCODE_DISABLE_DEFAULT_PLUGINS=", child_env)
        self.assertNotIn("OPENAI_API_KEY=", child_env)

    def test_run_announces_backend_and_resolved_model(self) -> None:
        # The console must state exactly which backend and model the loop is
        # about to spend budget on, including a model that came from
        # DEFAULT_MODELS rather than an explicit --model.
        opencode = self.run_ralph()
        self.assertEqual(opencode.returncode, 0, opencode.stderr)
        self.assertIn("ralph: backend opencode, model openai/gpt-5.6-sol", opencode.stderr)

        for path in self.calls.iterdir():
            path.unlink()

        claude = self.run_ralph(backend="claude")
        self.assertEqual(claude.returncode, 0, claude.stderr)
        self.assertIn("ralph: backend claude, model claude-opus-5", claude.stderr)

        for path in self.calls.iterdir():
            path.unlink()

        # An explicit --model is announced verbatim.
        requested = "claude-sonnet-4-6"
        explicit = self.run_ralph(
            "--model",
            requested,
            backend="claude",
            env={
                "FAKE_CLAUDE_EVENTS": self._claude_events(
                    "Work complete.\n<promise>COMPLETE</promise>", model=requested
                )
            },
        )
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        self.assertIn(f"ralph: backend claude, model {requested}", explicit.stderr)

    def test_success_without_marker_reports_exhausted_budget(self) -> None:
        result = self.run_ralph(
            env={
                "FAKE_EVENTS": self._events("Implemented and verified."),
                "FAKE_EXPORT": self._export("Implemented and verified."),
            }
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("iteration budget exhausted", result.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        self.assertEqual(json.loads((run_dir / "outcome.json").read_text())["outcome"], "budget_exhausted")

    def test_runs_fresh_sessions_until_early_completion_with_one_prompt_snapshot(self) -> None:
        sequence = self._sequence(
            [
                "Implemented child one.",
                "Implemented child two.",
                "No work remains.\n<promise>COMPLETE</promise>",
                "This iteration must not run.",
            ]
        )

        result = self.run_ralph(
            "--iterations",
            "4",
            env={"FAKE_MUTATE_PROMPT": str(self.prompt), "FAKE_SEQUENCE_DIR": str(sequence)},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.calls / "run-count").read_text().strip(), "3")
        composed_prompts = [(self.calls / f"stdin-{index}").read_text() for index in range(1, 4)]
        self.assertEqual(composed_prompts[0], composed_prompts[1])
        self.assertEqual(composed_prompts[1], composed_prompts[2])
        self.assertIn("explicit blocker evidence", composed_prompts[0])
        self.assertIn("<promise>NEEDS_INPUT</promise>", composed_prompts[0])
        self.assertEqual((self.calls / "auth-count").read_text().strip(), "3")
        # Each consumed iteration retains exactly one outcome entry, numbered in
        # order, and none carries a retry marker: the loop spends one session per
        # slot and never restarts a slot itself.
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        recorded = json.loads((run_dir / "outcome.json").read_text())["iterations"]
        self.assertEqual([entry["number"] for entry in recorded], [1, 2, 3])
        self.assertTrue(all("retried" not in entry for entry in recorded))

    def test_each_fresh_session_reproves_backend_trust(self) -> None:
        sequence = self._sequence(["First child complete.", "Second child complete."])
        opencode = self.run_ralph(
            "--iterations",
            "2",
            env={"FAKE_SEQUENCE_DIR": str(sequence)},
        )
        self.assertEqual(opencode.returncode, 1, opencode.stderr)
        self.assertEqual((self.calls / "auth-count").read_text().strip(), "2")
        opencode_calls = (self.calls / "opencode").read_text().splitlines()
        for command in ("--version", "--pure auth list", "--pure debug config", "--pure models openai"):
            self.assertEqual(opencode_calls.count(command), 2)

        for path in self.calls.iterdir():
            path.unlink()
        claude = self.run_ralph(
            "--iterations",
            "2",
            backend="claude",
            env={"FAKE_CLAUDE_EVENTS": self._claude_events("Child complete.")},
        )
        self.assertEqual(claude.returncode, 1, claude.stderr)
        self.assertEqual((self.calls / "claude-auth-count").read_text().strip(), "2")
        claude_calls = (self.calls / "claude").read_text().splitlines()
        self.assertEqual(claude_calls.count("--version"), 2)
        self.assertEqual(claude_calls.count("auth status"), 2)

    def test_synchronous_subagent_events_complete_normally(self) -> None:
        # Synchronous subagents emit task_started and task_notification but never
        # background_tasks_changed; the adapter ignores both informational events,
        # so a single-turn synchronous-agent iteration completes normally.
        events = self._claude_events("Work complete.\n<promise>COMPLETE</promise>").splitlines()
        started = json.dumps(
            {
                "type": "system",
                "subtype": "task_started",
                "session_id": "claude-session-1",
                "task_id": "t1",
            }
        )
        notification = json.dumps(
            {
                "type": "system",
                "subtype": "task_notification",
                "session_id": "claude-session-1",
                "task_id": "t1",
                "status": "completed",
            }
        )
        events.insert(1, started)  # after init
        events.insert(-1, notification)  # before the terminal result

        result = self.run_ralph(
            backend="claude",
            env={"FAKE_CLAUDE_EVENTS": "\n".join(events)},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Work complete.", result.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        self.assertEqual(json.loads((run_dir / "outcome.json").read_text())["outcome"], "complete")

    def test_between_iteration_auth_and_customization_mutation_stops_before_next_session(self) -> None:
        sequence = self._sequence(["First child complete.", "must not run"])
        mutation = self.base / "credentials-mutated"
        opencode = self.run_ralph(
            "--iterations",
            "2",
            env={
                "FAKE_AUTH_MUTATED_FILE": str(mutation),
                "FAKE_SEQUENCE_DIR": str(sequence),
            },
        )
        self.assertEqual(opencode.returncode, 2)
        self.assertIn("OpenAI OAuth credential", opencode.stderr)
        self.assertEqual((self.calls / "run-count").read_text().strip(), "1")
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        outcome = json.loads((run_dir / "outcome.json").read_text())
        self.assertEqual(len(outcome["iterations"]), 1)

        for path in self.calls.iterdir():
            path.unlink()
        hooks = self.repo / ".claude" / "hooks"
        claude = self.run_ralph(
            "--iterations",
            "2",
            backend="claude",
            env={
                "FAKE_CLAUDE_EVENTS": self._claude_events("First child complete."),
                "FAKE_CLAUDE_MUTATE_CUSTOMIZATION": str(hooks),
            },
        )
        self.assertEqual(claude.returncode, 2)
        self.assertIn("Claude customizations", claude.stderr)
        claude_calls = (self.calls / "claude").read_text().splitlines()
        self.assertEqual(sum(line.startswith("-p ") for line in claude_calls), 1)

    def test_iteration_budget_must_be_between_one_and_one_hundred(self) -> None:
        for budget in ("0", "101"):
            with self.subTest(budget=budget):
                result = self.run_ralph("--iterations", budget)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("between 1 and 100", result.stderr)

    def test_branch_changes_are_recorded_and_surfaced(self) -> None:
        result = self.run_ralph(env={"FAKE_BRANCH_CHANGE": "agent-branch"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("branch changed from main to agent-branch", result.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        self.assertIn("agent-branch", (run_dir / "git-status-final.txt").read_text())

    def test_dirty_worktree_warns_but_permits_the_run(self) -> None:
        # A dirty worktree is recorded and warned about but never refused.
        (self.repo / "uncommitted.txt").write_text("work in progress", encoding="utf-8")

        result = self.run_ralph()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("uncommitted changes", result.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        self.assertIn("uncommitted.txt", (run_dir / "git-status.txt").read_text())

    def test_a_completed_run_closes_the_iteration_and_summarises(self) -> None:
        # A successful run no longer exits silently: the Iteration closes with an
        # outcome block, and the run ends with a summary naming the git outcome and
        # the evidence path.
        result = self.run_ralph()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ralph: iteration 1 of 1 complete in", result.stderr)
        self.assertIn("session ses_1", result.stderr)
        self.assertIn("ralph: outcome run complete", result.stderr)
        self.assertIn("ralph: branch main", result.stderr)
        run_dir = next((self.repo.resolve() / ".git" / "ralph" / "runs").iterdir())
        self.assertIn(f"ralph: evidence {run_dir}", result.stderr)

    def test_the_trust_boundary_is_stated_once_its_proof_completes(self) -> None:
        result = self.run_ralph()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "ralph: trust boundary proven: subscription-only authentication, "
            "customization isolation, host isolation",
            result.stderr,
        )

    def test_budget_exhaustion_summarises_with_the_evidence_and_message(self) -> None:
        result = self.run_ralph(
            env={
                "FAKE_EVENTS": self._events("Implemented and verified."),
                "FAKE_EXPORT": self._export("Implemented and verified."),
            }
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("iteration budget exhausted", result.stderr)
        self.assertIn("Implemented and verified.", result.stderr)
        run_dir = next((self.repo.resolve() / ".git" / "ralph" / "runs").iterdir())
        self.assertIn(f"ralph: evidence {run_dir}", result.stderr)

    def test_the_summary_appears_on_the_handoff_path_before_the_banner(self) -> None:
        final = "<promise>NEEDS_INPUT</promise>\nShould I preserve the legacy file?"
        result = self.run_ralph(
            env={"FAKE_EVENTS": self._events(final), "FAKE_EXPORT": self._export(final)}
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        # The handoff banner is preserved, and the summary now accompanies it on
        # this terminal path too.
        self.assertIn("RALPH NEEDS OPERATOR", result.stderr)
        run_dir = next((self.repo.resolve() / ".git" / "ralph" / "runs").iterdir())
        self.assertIn(f"ralph: evidence {run_dir}", result.stderr)
        # The summary precedes the banner so the call-to-action stays last.
        self.assertLess(
            result.stderr.index(f"ralph: evidence {run_dir}"),
            result.stderr.index("RALPH NEEDS OPERATOR"),
        )


class IterationOutcomeNamingTest(RalphCliTestCase):
    """Findings 3, 6, 7 and 9 of the #36 review, at the black-box seam: an
    Iteration is named for what happened to *the Iteration*, every terminal path
    closes with an outcome block, an interrupted run is summarised in words, and
    the block shows the Backend's prose rather than Ralph's own protocol markers."""

    def _recorded(self) -> dict:
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        return json.loads((run_dir / "outcome.json").read_text())

    def test_an_ordinary_iteration_is_not_reported_as_budget_exhausted(self) -> None:
        # Finding 3: iterations 1 and 2 ended exactly as the Loop protocol says a
        # normal non-completing Iteration should -- "finishing that one child while
        # unblocked children still remain is a normal end of iteration" -- with the
        # budget intact, so neither may borrow the run-level word for running out
        # of it.
        sequence = self._sequence(
            [
                "Implemented child one.",
                "Implemented child two.",
                "No work remains.\n<promise>COMPLETE</promise>",
            ]
        )
        result = self.run_ralph(
            "--iterations", "3", env={"FAKE_SEQUENCE_DIR": str(sequence)}
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ralph: iteration 1 of 3 incomplete in", result.stderr)
        self.assertIn("ralph: iteration 2 of 3 incomplete in", result.stderr)
        self.assertIn("ralph: iteration 3 of 3 complete in", result.stderr)
        # The grep habit register G19 protects: a run whose budget was never
        # exhausted says nothing about a budget at all.
        self.assertNotIn("budget", result.stderr)
        recorded = self._recorded()
        self.assertEqual(
            [entry["outcome"] for entry in recorded["iterations"]],
            ["incomplete", "incomplete", "complete"],
        )
        self.assertEqual(recorded["outcome"], "complete")

    def test_budget_exhaustion_is_the_loops_own_word_and_greps_once(self) -> None:
        # The run-level word is the Loop's decision, taken when the budget actually
        # runs out, and no longer inherited from the last Iteration's return.
        sequence = self._sequence(["Implemented child one.", "Implemented child two."])
        result = self.run_ralph(
            "--iterations", "2", env={"FAKE_SEQUENCE_DIR": str(sequence)}
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        # J5: the phrase an operator greps for stays byte-identical, and now
        # matches exactly once -- at the summary, where the budget ran out.
        self.assertIn("iteration budget exhausted", result.stderr)
        self.assertEqual(result.stderr.count("budget"), 1)
        self.assertIn("ralph: iteration 1 of 2 incomplete in", result.stderr)
        self.assertIn("ralph: iteration 2 of 2 incomplete in", result.stderr)
        recorded = self._recorded()
        self.assertEqual(
            [entry["outcome"] for entry in recorded["iterations"]],
            ["incomplete", "incomplete"],
        )
        self.assertEqual(recorded["outcome"], "budget_exhausted")

    def test_an_iteration_that_hands_off_still_closes_with_its_outcome_block(self) -> None:
        # Finding 6: the one Iteration an operator most wants attributed is the one
        # that stopped the loop, and a handoff used to jump straight from the rule
        # to the run summary with no block, no duration and no session id at all.
        final = "<promise>NEEDS_INPUT</promise>\nShould I preserve the legacy file?"
        result = self.run_ralph(
            "--iterations",
            "2",
            env={"FAKE_EVENTS": self._events(final), "FAKE_EXPORT": self._export(final)},
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("ralph: iteration 1 of 2 needs_input in", result.stderr)
        self.assertIn("session ses_1", result.stderr)
        # The block closes the Iteration, so it prints before the summary and the
        # banner keep the last, most visible lines.
        self.assertLess(
            result.stderr.index("iteration 1 of 2 needs_input"),
            result.stderr.index("ralph: outcome "),
        )
        self.assertEqual(
            [entry["outcome"] for entry in self._recorded()["iterations"]], ["needs_input"]
        )

    def test_an_interrupted_run_is_summarised_in_words(self) -> None:
        # Finding 7: Ctrl-C is the commonest way an operator ends a multi-hour run,
        # and its outcome fell through to the bare `run ended: interrupted`.
        process = self.start_ralph(
            "--timeout",
            "0",
            env={"FAKE_EVENTS": self._events("Partial work"), "FAKE_SLEEP": "30"},
        )
        self._await_ready(self.calls / "env", process)
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=20)

        self.assertEqual(process.returncode, 2, stdout + stderr)
        self.assertNotIn("run ended: interrupted", stderr)
        self.assertIn("ralph: outcome run stopped: interrupted by the operator", stderr)
        # Finding 6 on the interrupted path too: the Iteration is attributed.
        self.assertIn("ralph: iteration 1 of 1 interrupted in", stderr)

    def test_the_outcome_block_shows_prose_not_the_protocols_own_markers(self) -> None:
        # Finding 9: #44 put a `<promise>STAGE: ...</promise>` line into essentially
        # every concluding message and the block printed it verbatim. The markers
        # are Ralph's own contract echoed back, not the Backend's prose; the outcome
        # word on the line above already reports what they signalled.
        final = (
            "<promise>STAGE: loading context</promise>\n"
            "Looked at the tracker and picked #7.\n"
            "<promise>COMPLETE</promise>"
        )
        result = self.run_ralph(
            env={"FAKE_EVENTS": self._events(final), "FAKE_EXPORT": self._export(final)}
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Looked at the tracker and picked #7.", result.stderr)
        self.assertNotIn("<promise>", result.stderr)
        self.assertNotIn("STAGE:", result.stderr)
        # The retained stream keeps the whole message, markers included: this is a
        # display change and never an artifact change (register G18).
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        self.assertIn("<promise>COMPLETE</promise>", (run_dir / "stdout.ndjson").read_text())

    def test_a_marker_the_parsers_read_as_prose_stays_prose_on_screen(self) -> None:
        # The block drops exactly what the parsers read as a signal and no more. A
        # marker mentioned inside a sentence, and one quoted inside a fenced block,
        # are prose to every parser -- neither completes the Iteration, hence
        # `incomplete` -- so they stay prose on screen and the console can never
        # disagree with the contract about what a declaration is.
        final = (
            "I was asked to emit <promise>COMPLETE</promise> once done.\n"
            "\n"
            "```\n"
            "<promise>COMPLETE</promise>\n"
            "```"
        )
        result = self.run_ralph(
            env={"FAKE_EVENTS": self._events(final), "FAKE_EXPORT": self._export(final)}
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("ralph: iteration 1 of 1 incomplete in", result.stderr)
        self.assertIn(
            "I was asked to emit <promise>COMPLETE</promise> once done.", result.stderr
        )

    def test_a_stop_between_iterations_closes_neither_twice_nor_early(self) -> None:
        # The Iteration that closes is the one the operator was told had started, and
        # it closes once. A power assertion lost between iterations stops the run
        # before the second Iteration's rule, so iteration 1 keeps its single block
        # and iteration 2 -- never announced -- gets none.
        result = self._run_guarded(
            "--iterations",
            "2",
            env={
                "FAKE_KILL_CAFFEINATE": "1",
                "FAKE_EVENTS": self._events("Partial work"),
                "FAKE_EXPORT": self._export("Partial work"),
            },
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stderr.count("iteration 1 of 2 incomplete in"), 1)
        self.assertNotIn("iteration 2 of 2", result.stderr)

    def test_a_failure_before_a_new_session_never_names_the_previous_one(self) -> None:
        # The block closing an Iteration that failed before its own session existed
        # must say so, not inherit the session id the Iteration before it established.
        sequence = self._sequence(["First child done.", "must not run"])
        result = self.run_ralph(
            "--iterations",
            "2",
            env={
                "FAKE_AUTH_MUTATED_FILE": str(self.base / "credentials-mutated"),
                "FAKE_SEQUENCE_DIR": str(sequence),
            },
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("ralph: iteration 1 of 2 incomplete in", result.stderr)
        self.assertIn("session ses_1", result.stderr)
        closing = next(
            line
            for line in result.stderr.splitlines()
            if line.startswith("ralph: iteration 2 of 2 backend_failure")
        )
        self.assertIn("no session id", closing)
        self.assertNotIn("ses_1", closing)
