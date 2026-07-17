"""Run loop and iteration budget: fresh-session cadence, budget bounds,
backend/model announcement, per-iteration trust re-proof, branch reporting."""

from __future__ import annotations

import json

from harness import RalphCliTestCase


class RunLoopTest(RalphCliTestCase):
    def test_exact_completion_runs_safely_and_retains_evidence(self) -> None:
        result = self.run_ralph()

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
        # init, and the iteration prompt must steer the model away from it.
        events = self._claude_events("Working.").splitlines()
        background = json.dumps(
            {
                "type": "system",
                "subtype": "background_tasks_changed",
                "session_id": "claude-session-1",
                "tasks": [{"task_id": "t1", "task_type": "local_agent", "description": "survey"}],
            }
        )
        events.insert(2, background)  # after init and the assistant turn, before result

        result = self.run_ralph(
            backend="claude",
            env={"FAKE_CLAUDE_EVENTS": "\n".join(events)},
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("RALPH NEEDS OPERATOR", result.stderr)
        self.assertIn("run synchronously", result.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        outcome = json.loads((run_dir / "outcome.json").read_text())
        self.assertEqual(outcome["outcome"], "backend_contract_failure")
        self.assertIn("background subagent", outcome["iterations"][0]["reason"])
        composed = json.loads((self.calls / "claude-stdin").read_text())
        self.assertIn("do not pass run_in_background", composed["message"]["content"])

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
