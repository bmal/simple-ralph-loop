"""Run loop and iteration budget: fresh-session cadence, budget bounds,
backend/model announcement, per-iteration trust re-proof, branch reporting."""

from __future__ import annotations

import json

from harness import RalphCliTestCase


class RunLoopTest(RalphCliTestCase):
    def test_exact_completion_runs_safely_and_retains_evidence(self) -> None:
        # An ambient opt-out must not disable OpenCode's built-in OAuth plugin in
        # the isolated child environment.
        result = self.run_ralph(env={"OPENCODE_DISABLE_DEFAULT_PLUGINS": "true"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Work complete.", result.stdout)
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
        self.assertIn("ralph: backend claude, model claude-opus-4-8", claude.stderr)

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

    def test_background_subagent_halts_with_a_synchronous_directive(self) -> None:
        # A subagent launched with run_in_background emits background_tasks_changed
        # and later forces a second init; Ralph must halt at the background launch
        # with a message that names the real cause, not the downstream duplicate
        # init, and the iteration prompt must steer the model away from it. This
        # backend launches one on every attempt, so the loop spends the allowance
        # on replacements first and the halt is what remains when they are gone --
        # a one-iteration budget does not shorten that, because replacements cost
        # no budget.
        result = self.run_ralph(
            backend="claude",
            env={"FAKE_CLAUDE_EVENTS": self._claude_background_events("claude-session-1")},
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("RALPH NEEDS OPERATOR", result.stderr)
        self.assertIn("run synchronously", result.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        outcome = json.loads((run_dir / "outcome.json").read_text())
        self.assertEqual(outcome["outcome"], "backend_contract_failure")
        self.assertIn("background subagent", outcome["iterations"][0]["reason"])
        # Three attempts at the single iteration, all charged to it, and no
        # automatic replacement iteration is offered because the budget is spent.
        self.assertEqual([entry["number"] for entry in outcome["iterations"]], [1, 1, 1])
        self.assertIn("No automatic replacement iteration remains.", result.stderr)
        composed = json.loads((self.calls / "claude-stdin").read_text())
        # The tool defaults to background, so the directive must demand the
        # synchronous parameter explicitly rather than tell the model to omit it.
        self.assertIn("run_in_background: false", composed["message"]["content"])

    def test_a_lost_attempt_is_replaced_without_spending_budget(self) -> None:
        # A lost attempt returns no outcome, so it must not consume its iteration:
        # the same slot runs a fresh session whose prompt carries the backend's
        # correction, and a completion there finishes the run normally. Both
        # attempts belong to iteration 1 and each keeps its own evidence.
        sequence = self._claude_sequence(
            [
                self._claude_background_events("claude-session-1"),
                self._claude_events(
                    "Work complete.\n<promise>COMPLETE</promise>",
                    session_id="claude-session-2",
                ),
            ]
        )

        result = self.run_ralph(
            "--iterations",
            "3",
            backend="claude",
            env={"FAKE_CLAUDE_SEQUENCE_DIR": str(sequence)},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "iteration 1 was lost and is being retried without spending budget "
            "(replacement 1 of 2)",
            result.stderr,
        )
        # The replacement is announced as another attempt at the same iteration,
        # never as iteration 2.
        self.assertIn("ralph: iteration 1 of 3 (attempt 2)", result.stderr)
        self.assertNotIn("ralph: iteration 2 of 3", result.stderr)
        self.assertNotIn("RALPH NEEDS OPERATOR", result.stderr)
        self.assertEqual((self.calls / "claude-run-count").read_text().strip(), "2")
        first = json.loads((self.calls / "claude-stdin-1").read_text())["message"]["content"]
        second = json.loads((self.calls / "claude-stdin-2").read_text())["message"]["content"]
        self.assertNotIn("previous iteration was killed", first)
        self.assertIn("previous iteration was killed", second)
        self.assertIn("Implement the selected issue.", second)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        outcome = json.loads((run_dir / "outcome.json").read_text())
        self.assertEqual(outcome["outcome"], "complete")
        self.assertEqual(outcome["session_id"], "claude-session-2")
        lost, replacement = outcome["iterations"]
        self.assertEqual(lost["outcome"], "backend_contract_failure")
        self.assertIs(lost["retried"], True)
        self.assertEqual(lost["session_id"], "claude-session-1")
        self.assertIn("background subagent", lost["reason"])
        self.assertEqual(replacement["outcome"], "complete")
        # Both attempts are iteration 1: the loss spent no budget.
        self.assertEqual([entry["number"] for entry in outcome["iterations"]], [1, 1])
        # The replacement nests its evidence rather than overwriting the lost
        # attempt's retained output.
        self.assertIn("background_tasks_changed", (run_dir / "stdout.ndjson").read_text())
        self.assertIn(
            "<promise>COMPLETE</promise>",
            (run_dir / "attempt-002" / "stdout.ndjson").read_text(),
        )
        self.assertEqual(
            json.loads((run_dir / "attempt-002" / "session.json").read_text())["session_id"],
            "claude-session-2",
        )

    def test_consecutive_lost_attempts_exhaust_the_retry_allowance(self) -> None:
        # Lost attempts cost no budget, so the allowance is the only thing that
        # bounds a backend failing the same way forever: the third attempt at one
        # iteration hands off. That handoff still charges the started iteration,
        # and offers the rest of the budget back in the restart command.
        sequence = self._claude_sequence(
            [self._claude_background_events(f"claude-session-{index}") for index in range(1, 4)]
        )

        result = self.run_ralph(
            "--iterations",
            "6",
            backend="claude",
            env={"FAKE_CLAUDE_SEQUENCE_DIR": str(sequence)},
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("replacement 1 of 2", result.stderr)
        self.assertIn("replacement 2 of 2", result.stderr)
        self.assertNotIn("replacement 3 of 2", result.stderr)
        self.assertIn("RALPH NEEDS OPERATOR", result.stderr)
        # All three attempts were iteration 1, and the handoff charges it.
        self.assertIn("ralph: iteration 1 of 6 (attempt 3)", result.stderr)
        self.assertNotIn("ralph: iteration 2 of 6", result.stderr)
        self.assertIn("iterations remaining: 5", result.stderr)
        self.assertEqual((self.calls / "claude-run-count").read_text().strip(), "3")
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        outcome = json.loads((run_dir / "outcome.json").read_text())
        self.assertEqual(outcome["outcome"], "backend_contract_failure")
        self.assertEqual(outcome["session_id"], "claude-session-3")
        self.assertEqual(len(outcome["iterations"]), 3)
        self.assertEqual([entry["number"] for entry in outcome["iterations"]], [1, 1, 1])
        # Only the absorbed losses are marked retried; the one that stopped the
        # run is an ordinary handoff.
        self.assertEqual(
            [entry.get("retried", False) for entry in outcome["iterations"]],
            [True, True, False],
        )
        # Every attempt kept its own evidence.
        self.assertTrue((run_dir / "attempt-002" / "stdout.ndjson").exists())
        self.assertTrue((run_dir / "attempt-003" / "stdout.ndjson").exists())

    def test_a_consumed_iteration_restores_the_retry_allowance(self) -> None:
        # The allowance is per iteration. An attempt that returns an outcome
        # consumes the slot and clears both the correction and the count, so a
        # loss in a later iteration is a fresh one, not the tail of an earlier
        # streak.
        sequence = self._claude_sequence(
            [
                self._claude_background_events("claude-session-1"),
                self._claude_events("Working on it.", session_id="claude-session-2"),
                self._claude_background_events("claude-session-3"),
                self._claude_events(
                    "Work complete.\n<promise>COMPLETE</promise>",
                    session_id="claude-session-4",
                ),
            ]
        )

        result = self.run_ralph(
            "--iterations",
            "4",
            backend="claude",
            env={"FAKE_CLAUDE_SEQUENCE_DIR": str(sequence)},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        # Both losses report as the first of the allowance, not 1 then 2.
        self.assertEqual(result.stderr.count("replacement 1 of 2"), 2)
        self.assertNotIn("replacement 2 of 2", result.stderr)
        # Four sessions, but only two iterations were ever spent.
        self.assertIn("ralph: iteration 1 of 4 (attempt 2)", result.stderr)
        self.assertIn("ralph: iteration 2 of 4 (attempt 2)", result.stderr)
        self.assertNotIn("ralph: iteration 3 of 4", result.stderr)
        # The correction rides only the replacement, never the iteration after it.
        contents = [
            json.loads((self.calls / f"claude-stdin-{index}").read_text())["message"]["content"]
            for index in range(1, 5)
        ]
        self.assertEqual(
            ["previous iteration was killed" in content for content in contents],
            [False, True, False, True],
        )

    def test_synchronous_subagent_events_pass_through_the_background_guard(self) -> None:
        # Synchronous subagents emit task_started and task_notification but never
        # background_tasks_changed; those events must pass through untouched so a
        # legitimate synchronous-agent iteration completes normally.
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
        self.assertIn("Work complete.", result.stdout)
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
