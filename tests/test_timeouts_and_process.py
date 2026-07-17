"""Iteration timeouts, process-group escalation, and the loop-wide
caffeinate power assertion."""

from __future__ import annotations

import json
import signal
import subprocess
import time

from harness import RalphCliTestCase
from ralph import process


class TimeoutAndProcessControlTest(RalphCliTestCase):
    def test_timeout_defaults_to_60_minutes_and_accepts_positive_or_zero_seconds(self) -> None:
        default = self.run_ralph()
        self.assertEqual(default.returncode, 0, default.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        self.assertEqual(json.loads((run_dir / "options.json").read_text())["timeout"], 3600)

        for path in self.calls.iterdir():
            path.unlink()
        disabled = self.run_ralph("--timeout", "0")
        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        run_dirs = sorted((self.repo / ".git" / "ralph" / "runs").iterdir())
        self.assertEqual(json.loads((run_dirs[-1] / "options.json").read_text())["timeout"], 0)

        for value in ("-1", "nan", "inf"):
            with self.subTest(value=value):
                invalid = self.run_ralph("--timeout", value)
                self.assertNotEqual(invalid.returncode, 0)
                self.assertIn("timeout must be zero or positive", invalid.stderr)

    def test_timeout_gracefully_escalates_and_hands_off_a_started_session(self) -> None:
        # The deadline is small only to force a timeout quickly; it must still
        # comfortably exceed the cold-start of the launch chain (caffeinate ->
        # sandbox-exec -> backend) so the started session's metadata is captured
        # before the timer fires. The 30s backend sleep dwarfs it either way.
        result = self.run_ralph(
            "--iterations",
            "2",
            "--timeout",
            "0.5",
            env={
                "FAKE_EVENTS": self._events("Partial work"),
                "FAKE_SLEEP": "30",
                "FAKE_IGNORE_SIGNALS": "1",
            },
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("iteration timed out", result.stderr)
        self.assertIn("--session ses_1", result.stderr)
        self.assertIn("iterations remaining: 1", result.stderr)
        self.assertIn("--timeout 0.5", result.stderr)
        self.assertEqual((self.calls / "signals").read_text(), "INTTERM")
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        outcome = json.loads((run_dir / "outcome.json").read_text())
        self.assertEqual(outcome["outcome"], "timeout")
        self.assertEqual(outcome["iterations"][0]["session_id"], "ses_1")

    def test_claude_timeout_uses_the_same_resumable_handoff(self) -> None:
        result = self.run_ralph(
            "--timeout",
            "0.1",
            backend="claude",
            env={
                "FAKE_CLAUDE_EVENTS": self._claude_events("unused").splitlines()[0],
                "FAKE_CLAUDE_IGNORE_SIGNALS": "1",
                "FAKE_CLAUDE_SLEEP": "30",
            },
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Claude iteration timed out", result.stderr)
        self.assertIn("ralph resume --backend claude", result.stderr)
        self.assertIn("--session claude-session-1", result.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        session = json.loads((run_dir / "session.json").read_text())
        self.assertEqual(session["session_id"], "claude-session-1")
        self.assertFalse(session["final_result_received"])
        self.assertEqual((self.calls / "claude-signals").read_text(), "INTTERM")

    def test_claude_timeout_is_not_misreported_when_the_interrupt_yields_an_error_result(self) -> None:
        # The real Claude CLI answers Ralph's timeout interrupt with a final
        # error result event (subtype error_during_execution) before exiting.
        # That artifact of Ralph's own stop must surface as a timeout handoff,
        # not as a backend contract failure.
        error_result = json.dumps(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "session_id": "claude-session-1",
            }
        )
        result = self.run_ralph(
            "--timeout",
            "0.1",
            backend="claude",
            env={
                "FAKE_CLAUDE_EVENTS": self._claude_events("unused").splitlines()[0],
                "FAKE_CLAUDE_ERROR_RESULT_ON_INT": error_result,
            },
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Claude iteration timed out", result.stderr)
        self.assertNotIn("unsuccessful result", result.stderr)
        self.assertIn("ralph resume --backend claude", result.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        outcome = json.loads((run_dir / "outcome.json").read_text())
        self.assertEqual(outcome["outcome"], "timeout")
        self.assertEqual(outcome["iterations"][0]["outcome"], "timeout")
        session = json.loads((run_dir / "session.json").read_text())
        self.assertEqual(session["session_id"], "claude-session-1")
        self.assertFalse(session["final_result_received"])

    def test_timeout_before_session_metadata_still_consumes_the_started_iteration(self) -> None:
        result = self.run_ralph(
            "--timeout",
            "0.1",
            env={"FAKE_EVENTS": json.dumps({"type": "status"}), "FAKE_SLEEP": "30"},
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("before session metadata was received", result.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        outcome = json.loads((run_dir / "outcome.json").read_text())
        self.assertEqual(outcome["outcome"], "timeout")
        self.assertEqual(len(outcome["iterations"]), 1)
        self.assertIsNone(outcome["iterations"][0]["session_id"])

    def test_opencode_session_verification_obeys_the_iteration_deadline(self) -> None:
        # The run emits its session immediately; the export verification then
        # sleeps past the deadline. The budget must clear the launch chain's
        # cold-start (now caffeinate -> sandbox-exec -> backend) so the session
        # is captured before the export step times out.
        result = self.run_ralph(
            "--timeout",
            "0.5",
            env={"FAKE_EXPORT_SLEEP": "30"},
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("OpenCode iteration timed out", result.stderr)
        self.assertIn("--session ses_1", result.stderr)

    def test_second_interrupt_force_kills_and_hands_off_promptly(self) -> None:
        process = subprocess.Popen(
            self._command("run", "--timeout", "0"),
            cwd=self.base,
            env=self._environment(
                {
                    "FAKE_EVENTS": self._events("Partial work"),
                    "FAKE_SLEEP": "30",
                    "FAKE_IGNORE_SIGNALS": "1",
                }
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self._await_ready(self.calls / "env", process)

        started = time.monotonic()
        process.send_signal(signal.SIGINT)
        time.sleep(0.1)
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=5)

        self.assertEqual(process.returncode, 2, stdout + stderr)
        self.assertLess(time.monotonic() - started, 2)
        self.assertIn("interrupted by user", stderr)
        self.assertIn("--session ses_1", stderr)

    def test_timeout_kills_departed_leader_with_pipe_holding_descendant(self) -> None:
        # The fake leader exits immediately but a descendant keeps the stdout and
        # stderr pipes open. Ralph must terminate the whole group on timeout
        # rather than block forever waiting on the inherited pipes.
        result = self._run_guarded(
            "--timeout",
            "0.3",
            env={"FAKE_EVENTS": self._events("Partial work"), "FAKE_ORPHAN_SLEEP": "30"},
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("iteration timed out", result.stderr)
        self.assertIn("--session ses_1", result.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        outcome = json.loads((run_dir / "outcome.json").read_text())
        self.assertEqual(outcome["outcome"], "timeout")

    def test_claude_timeout_kills_departed_leader_with_pipe_holding_descendant(self) -> None:
        result = self._run_guarded(
            "--timeout",
            "0.3",
            backend="claude",
            env={
                "FAKE_CLAUDE_EVENTS": self._claude_events("unused").splitlines()[0],
                "FAKE_CLAUDE_ORPHAN_SLEEP": "30",
            },
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("Claude iteration timed out", result.stderr)
        self.assertIn("--session claude-session-1", result.stderr)

    def test_pre_metadata_timeout_shows_operator_banner_with_remaining_budget(self) -> None:
        result = self._run_guarded(
            "--iterations",
            "2",
            "--timeout",
            "0.3",
            env={"FAKE_EVENTS": json.dumps({"type": "status"}), "FAKE_SLEEP": "30"},
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("RALPH NEEDS OPERATOR", result.stderr)
        self.assertIn("before session metadata was received", result.stderr)
        # No session id exists, so the manual resume line is omitted entirely...
        self.assertNotIn("manual resume:", result.stderr)
        # ...but the exact remaining-budget command still appears.
        self.assertIn("iterations remaining: 1", result.stderr)
        self.assertIn("continue Ralph:", result.stderr)
        self.assertIn("--iterations 1", result.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        outcome = json.loads((run_dir / "outcome.json").read_text())
        self.assertEqual(outcome["outcome"], "timeout")
        self.assertEqual(len(outcome["iterations"]), 1)
        self.assertIsNone(outcome["iterations"][0]["session_id"])

    def test_caffeinate_assertion_covers_the_complete_loop(self) -> None:
        result = self.run_ralph()

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.calls / "caffeinate").read_text().splitlines()
        self.assertTrue(any(line.startswith("-im -w ") for line in calls), calls)

    def test_failed_loop_caffeinate_assertion_stops_before_backend_preflight(self) -> None:
        result = self.run_ralph(env={"FAKE_CAFFEINATE_FAIL": "1"})

        self.assertEqual(result.returncode, 2)
        self.assertIn("caffeinate exited during startup", result.stderr)
        self.assertFalse((self.calls / "opencode").exists())

    def test_absolute_caffeinate_is_not_path_shadowed(self) -> None:
        system = self.base / "system"
        system.mkdir()
        good = system / "caffeinate"
        good.write_text((self.bin / "caffeinate").read_text(), encoding="utf-8")
        good.chmod(0o755)
        # A hostile caffeinate earlier on PATH would break the run if consulted.
        self._script(
            "caffeinate",
            """
            printf 'shadow\\n' >> "$FAKE_CALLS/caffeinate-shadow"
            exit 13
            """,
        )

        result = self.run_ralph(env={"RALPH_CAFFEINATE": str(good)})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.calls / "caffeinate-shadow").exists())
        self.assertIn("-im", (self.calls / "caffeinate").read_text())

    def test_caffeinate_runs_by_absolute_path_when_absent_from_path(self) -> None:
        system = self.base / "system"
        system.mkdir()
        good = system / "caffeinate"
        good.write_text((self.bin / "caffeinate").read_text(), encoding="utf-8")
        good.chmod(0o755)
        (self.bin / "caffeinate").unlink()

        result = self.run_ralph(env={"RALPH_CAFFEINATE": str(good)})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-im", (self.calls / "caffeinate").read_text())

    def test_lost_loop_caffeinate_assertion_stops_safely(self) -> None:
        result = self._run_guarded(
            "--iterations",
            "2",
            env={
                "FAKE_CAFFEINATE_DIE": "0.3",
                "FAKE_EVENTS": self._events("Partial work"),
                "FAKE_EXPORT": self._export("Partial work"),
                "FAKE_SLEEP": "0.6",
            },
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("caffeinate", result.stderr)
        self.assertIn("exited unexpectedly", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        outcome = json.loads((run_dir / "outcome.json").read_text())
        # The first iteration ran and its evidence is retained before stopping.
        self.assertEqual(len(outcome["iterations"]), 1)

    def test_timeout_upper_bound_keeps_backend_limits_subordinate(self) -> None:
        over = self.run_ralph("--timeout", str(process.MAX_ITERATION_TIMEOUT_SECONDS + 1))
        self.assertNotEqual(over.returncode, 0)
        self.assertIn("must not exceed", over.stderr)
        self.assertFalse((self.calls / "opencode").exists())

        at_max = self.run_ralph("--timeout", str(process.MAX_ITERATION_TIMEOUT_SECONDS))
        self.assertEqual(at_max.returncode, 0, at_max.stderr)
        self.assertIn(
            "OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS=2147483647",
            (self.calls / "env").read_text(),
        )

        for path in self.calls.iterdir():
            path.unlink()
        disabled = self.run_ralph("--timeout", "0")
        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        # Backend limits stay pinned at their maximum even with Ralph's timer off.
        self.assertIn(
            "OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS=2147483647",
            (self.calls / "env").read_text(),
        )

    def test_claude_backend_limits_stay_subordinate_when_timeout_disabled(self) -> None:
        result = self.run_ralph("--timeout", "0", backend="claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BASH_MAX_TIMEOUT_MS=2147483647", (self.calls / "claude-env").read_text())
