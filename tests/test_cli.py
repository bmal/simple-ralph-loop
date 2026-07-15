from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RalphCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
            cwd=self.repo,
            check=True,
        )
        self.prompt = self.base / "prompt.md"
        self.prompt.write_text("Implement the selected issue.\n", encoding="utf-8")
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.calls = self.base / "calls"
        self.calls.mkdir()
        self._write_fakes()

    def _script(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/sh\nset -eu\n" + textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o755)

    def _write_fakes(self) -> None:
        self._script(
            "gh",
            """
            printf '%s\\n' "$*" >> "$FAKE_CALLS/gh"
            test "${FAKE_GH_FAIL:-0}" = "0" || exit 1
            case "$1 $2" in
              "auth status") exit 0 ;;
              "repo view") printf '%s\\n' '{"url":"https://github.com/example/project"}' ;;
              *) exit 2 ;;
            esac
            """,
        )
        self._script(
            "caffeinate",
            """
            printf '%s\\n' "$*" >> "$FAKE_CALLS/caffeinate"
            test "$1" = "-im"
            shift
            if test "${1:-}" = "-w"; then
              test "${FAKE_CAFFEINATE_FAIL:-0}" = "0" || exit 9
              while kill -0 "$2" 2>/dev/null; do sleep 0.02; done
              exit 0
            fi
            exec "$@"
            """,
        )
        self._script(
            "opencode",
            """
            printf '%s\\n' "$*" >> "$FAKE_CALLS/opencode"
            case "$*" in
              "--version") printf '%s\\n' "${FAKE_VERSION:-1.17.20}" ;;
              "--pure auth list") printf '%s\\n' "${FAKE_AUTH:-OpenAI oauth}" ;;
              "--pure debug config") printf '%s\\n' "${FAKE_CONFIG}" ;;
              "--pure models openai") printf '%s\\n' "${FAKE_MODELS:-openai/gpt-5.6-sol}" ;;
              "--pure export "*)
                if test -n "${FAKE_EXPORT_SLEEP:-}"; then
                  sleep "$FAKE_EXPORT_SLEEP"
                fi
                if test -n "${FAKE_SEQUENCE_DIR:-}"; then
                  session_id=${3}
                  cat "$FAKE_SEQUENCE_DIR/export-$session_id"
                else
                  printf '%s\\n' "${FAKE_EXPORT}"
                fi
                ;;
              *" run "*)
                if test -n "${FAKE_SEQUENCE_DIR:-}"; then
                  count_file="$FAKE_CALLS/run-count"
                  count=0
                  test ! -f "$count_file" || count=$(cat "$count_file")
                  count=$((count + 1))
                  printf '%s\\n' "$count" > "$count_file"
                  cat > "$FAKE_CALLS/stdin-$count"
                  cat "$FAKE_SEQUENCE_DIR/events-$count"
                else
                  cat > "$FAKE_CALLS/stdin"
                  printf '%s\\n' "${FAKE_EVENTS}"
                fi
                env | sort > "$FAKE_CALLS/env"
                if test "${FAKE_IGNORE_SIGNALS:-0}" = "1"; then
                  trap 'printf INT >> "$FAKE_CALLS/signals"' INT
                  trap 'printf TERM >> "$FAKE_CALLS/signals"' TERM
                fi
                if test -n "${FAKE_SLEEP:-}"; then
                  if test "${FAKE_IGNORE_SIGNALS:-0}" = "1"; then
                    while :; do sleep "$FAKE_SLEEP" || true; done
                  else
                    sleep "$FAKE_SLEEP"
                  fi
                fi
                if test -n "${FAKE_BLOCK_FILE:-}"; then
                  : > "$FAKE_BLOCK_FILE.ready"
                  while test -e "$FAKE_BLOCK_FILE"; do sleep 0.05; done
                fi
                if test -n "${FAKE_MUTATE_PROMPT:-}"; then
                  printf '%s\\n' 'mutated by first session' > "$FAKE_MUTATE_PROMPT"
                fi
                if test -n "${FAKE_BRANCH_CHANGE:-}"; then
                  git checkout -b "$FAKE_BRANCH_CHANGE" >/dev/null 2>&1
                fi
                printf '%s\\n' "backend diagnostic" >&2
                exit "${FAKE_EXIT:-0}"
                ;;
              *) exit 2 ;;
            esac
            """,
        )
        self._script(
            "claude",
            """
            printf '%s\n' "$*" >> "$FAKE_CALLS/claude"
            case "$*" in
              "--version") printf '%s\n' "${FAKE_CLAUDE_VERSION:-2.1.208 (Claude Code)}" ;;
              "auth status")
                env | sort > "$FAKE_CALLS/claude-auth-env"
                printf '%s\n' "${FAKE_CLAUDE_AUTH}"
                ;;
              "-p "*)
                cat > "$FAKE_CALLS/claude-stdin"
                env | sort > "$FAKE_CALLS/claude-env"
                printf '%s\n' "${FAKE_CLAUDE_EVENTS}"
                if test "${FAKE_CLAUDE_IGNORE_SIGNALS:-0}" = "1"; then
                  trap 'printf INT >> "$FAKE_CALLS/claude-signals"' INT
                  trap 'printf TERM >> "$FAKE_CALLS/claude-signals"' TERM
                fi
                if test -n "${FAKE_CLAUDE_SLEEP:-}"; then
                  if test "${FAKE_CLAUDE_IGNORE_SIGNALS:-0}" = "1"; then
                    while :; do sleep "$FAKE_CLAUDE_SLEEP" || true; done
                  else
                    sleep "$FAKE_CLAUDE_SLEEP"
                  fi
                fi
                printf '%s\n' "claude diagnostic" >&2
                exit "${FAKE_CLAUDE_EXIT:-0}"
                ;;
              *) exit 2 ;;
            esac
            """,
        )

    def _events(self, text: str, model: str = "gpt-5.6-sol", session_id: str = "ses_1") -> str:
        del model
        return json.dumps(
            {
                "type": "text",
                "sessionID": session_id,
                "part": {
                    "id": "part_1",
                    "sessionID": session_id,
                    "messageID": "msg_1",
                    "type": "text",
                    "text": text,
                    "time": {"start": 1, "end": 2},
                },
            }
        )

    def _export(self, text: str, model: str = "gpt-5.6-sol", session_id: str = "ses_1") -> str:
        return json.dumps(
            {
                "info": {"id": session_id},
                "messages": [
                    {
                        "info": {
                            "id": "msg_1",
                            "sessionID": session_id,
                            "role": "assistant",
                            "providerID": "openai",
                            "modelID": model,
                        },
                        "parts": [{"id": "part_1", "type": "text", "text": text}],
                    }
                ],
            }
        )

    def _sequence(self, results: list[str]) -> Path:
        sequence = self.base / "sequence"
        sequence.mkdir()
        for index, text in enumerate(results, 1):
            session_id = f"ses_{index}"
            (sequence / f"events-{index}").write_text(
                self._events(text, session_id=session_id) + "\n", encoding="utf-8"
            )
            (sequence / f"export-{session_id}").write_text(
                self._export(text, session_id=session_id) + "\n", encoding="utf-8"
            )
        return sequence

    def _config(self) -> str:
        return json.dumps(
            {
                "model": "openai/gpt-5.6-sol",
                "small_model": "openai/gpt-5.6-sol",
                "enabled_providers": ["openai"],
                "provider": {"openai": {"options": {"timeout": False}}},
                "mcp": {},
                "plugin": [],
                "share": "disabled",
                "autoupdate": False,
                "formatter": False,
                "lsp": False,
            }
        )

    def _claude_events(
        self,
        text: str,
        model: str = "claude-opus-4-8",
        session_id: str = "claude-session-1",
    ) -> str:
        events = [
            {
                "type": "system",
                "subtype": "init",
                "session_id": session_id,
                "apiKeySource": "oauth",
                "model": model,
                "permissionMode": "bypassPermissions",
                "tools": ["Bash", "Read", "Edit"],
                "mcp_servers": [],
                "skills": ["implement"],
                "plugins": [],
            },
            {
                "type": "assistant",
                "session_id": session_id,
                "message": {
                    "id": "msg_claude_1",
                    "role": "assistant",
                    "model": model,
                    "content": [{"type": "text", "text": text}],
                },
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": session_id,
                "result": text,
                "modelUsage": {model: {"inputTokens": 1, "outputTokens": 1}},
            },
        ]
        return "\n".join(json.dumps(event) for event in events)

    def _environment(self, env: dict[str, str] | None = None) -> dict[str, str]:
        child_env = os.environ.copy()
        child_env.update(
            {
                "PATH": f"{self.bin}:{child_env['PATH']}",
                "PYTHONPATH": str(ROOT / "src"),
                "FAKE_CALLS": str(self.calls),
                "FAKE_CONFIG": self._config(),
                "FAKE_EVENTS": self._events("Work complete.\n<promise>COMPLETE</promise>"),
                "FAKE_EXPORT": self._export("Work complete.\n<promise>COMPLETE</promise>"),
                "FAKE_CLAUDE_AUTH": json.dumps(
                    {
                        "loggedIn": True,
                        "authMethod": "claude.ai",
                        "apiProvider": "firstParty",
                        "subscriptionType": "max",
                    }
                ),
                "FAKE_CLAUDE_EVENTS": self._claude_events(
                    "Work complete.\n<promise>COMPLETE</promise>"
                ),
            }
        )
        if env:
            child_env.update(env)
        return child_env

    def _command(
        self,
        command: str = "run",
        *extra: str,
        worktree: Path | None = None,
        backend: str = "opencode",
    ) -> list[str]:
        selected_worktree = worktree or self.repo
        if command == "clean":
            return [
                sys.executable,
                "-m",
                "ralph.cli",
                "clean",
                "--worktree",
                str(selected_worktree),
                *extra,
            ]
        return [
            sys.executable,
            "-m",
            "ralph.cli",
            "run",
            str(self.prompt),
            "--backend",
            backend,
            "--iterations",
            "1",
            "--worktree",
            str(selected_worktree),
            *extra,
        ]

    def run_ralph(
        self,
        *extra: str,
        env: dict[str, str] | None = None,
        backend: str = "opencode",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self._command("run", *extra, backend=backend),
            cwd=self.base,
            env=self._environment(env),
            text=True,
            capture_output=True,
        )

    def clean_ralph(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self._command("clean"),
            cwd=self.base,
            env=self._environment(),
            text=True,
            capture_output=True,
        )

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

    def test_iteration_budget_must_be_between_one_and_one_hundred(self) -> None:
        for budget in ("0", "101"):
            with self.subTest(budget=budget):
                result = self.run_ralph("--iterations", budget)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("between 1 and 100", result.stderr)

    def test_timeout_defaults_to_45_minutes_and_accepts_positive_or_zero_seconds(self) -> None:
        default = self.run_ralph()
        self.assertEqual(default.returncode, 0, default.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        self.assertEqual(json.loads((run_dir / "options.json").read_text())["timeout"], 2700)

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
        result = self.run_ralph(
            "--iterations",
            "2",
            "--timeout",
            "0.1",
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
        self.assertIn("--timeout 0.1", result.stderr)
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
        self.assertIn("--resume claude-session-1", result.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        session = json.loads((run_dir / "session.json").read_text())
        self.assertEqual(session["session_id"], "claude-session-1")
        self.assertFalse(session["final_result_received"])
        self.assertEqual((self.calls / "claude-signals").read_text(), "INTTERM")

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
        result = self.run_ralph(
            "--timeout",
            "0.1",
            env={"FAKE_EXPORT_SLEEP": "30"},
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("OpenCode iteration timed out", result.stderr)
        self.assertIn("--session ses_1", result.stderr)

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
        for _ in range(100):
            if (self.calls / "env").exists():
                break
            time.sleep(0.02)
        self.assertTrue((self.calls / "env").exists(), "backend did not start")

        started = time.monotonic()
        process.send_signal(signal.SIGINT)
        time.sleep(0.1)
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=5)

        self.assertEqual(process.returncode, 2, stdout + stderr)
        self.assertLess(time.monotonic() - started, 2)
        self.assertIn("interrupted by user", stderr)
        self.assertIn("--session ses_1", stderr)

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
        self.assertIn("/usr/bin/caffeinate -im opencode", result.stderr)
        self.assertIn("--session ses_1", result.stderr)
        self.assertIn("--model openai/gpt-5.6-sol --auto", result.stderr)
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
        self.assertIn("/usr/bin/caffeinate -im claude --resume claude-session-1", result.stderr)
        self.assertIn("--dangerously-skip-permissions", result.stderr)

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
        self.assertIn("--resume claude-session-1", malformed_result.stderr)
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

    def test_live_lock_refuses_a_second_loop_and_dead_owner_is_recovered(self) -> None:
        blocker = self.base / "blocked"
        blocker.touch()
        first_calls = self.base / "first-calls"
        first_calls.mkdir()
        first = subprocess.Popen(
            self._command(),
            cwd=self.base,
            env=self._environment({"FAKE_BLOCK_FILE": str(blocker), "FAKE_CALLS": str(first_calls)}),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: first.poll() is None and first.kill())
        ready = Path(f"{blocker}.ready")
        for _ in range(100):
            if ready.exists():
                break
            time.sleep(0.02)
        self.assertTrue(ready.exists(), "first loop did not reach the backend")

        second = self.run_ralph()
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("already running", second.stderr)
        cleaning = self.clean_ralph()
        self.assertNotEqual(cleaning.returncode, 0)
        self.assertIn("already running", cleaning.stderr)

        first.kill()
        first.communicate(timeout=5)
        blocker.unlink()
        recovered = self.run_ralph()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)

    def test_clean_removes_only_selected_repository_ralph_state(self) -> None:
        result = self.run_ralph()
        self.assertEqual(result.returncode, 0, result.stderr)
        source = self.repo / "keep.txt"
        source.write_text("source", encoding="utf-8")
        backend_state = self.base / "opencode-session"
        backend_state.write_text("transcript", encoding="utf-8")

        cleaned = self.clean_ralph()

        self.assertEqual(cleaned.returncode, 0, cleaned.stderr)
        self.assertFalse((self.repo / ".git" / "ralph").exists())
        self.assertEqual(list((self.repo / ".git").glob("ralph*")), [])
        self.assertEqual(source.read_text(), "source")
        self.assertEqual(backend_state.read_text(), "transcript")

    def test_linked_worktrees_have_independent_locks(self) -> None:
        tracked = self.repo / "tracked.txt"
        tracked.write_text("initial", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.repo, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Ralph Test",
                "-c",
                "user.email=ralph@example.invalid",
                "commit",
                "-m",
                "initial",
            ],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        other = self.base / "other-worktree"
        subprocess.run(
            ["git", "worktree", "add", "-b", "other", str(other)],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        blocker = self.base / "worktree-blocked"
        blocker.touch()
        first_calls = self.base / "worktree-first-calls"
        first_calls.mkdir()
        first = subprocess.Popen(
            self._command(),
            cwd=self.base,
            env=self._environment({"FAKE_BLOCK_FILE": str(blocker), "FAKE_CALLS": str(first_calls)}),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: first.poll() is None and first.kill())
        ready = Path(f"{blocker}.ready")
        for _ in range(100):
            if ready.exists():
                break
            time.sleep(0.02)
        self.assertTrue(ready.exists(), "first worktree did not reach the backend")

        independent = subprocess.run(
            self._command(worktree=other),
            cwd=self.base,
            env=self._environment(),
            text=True,
            capture_output=True,
        )

        self.assertEqual(independent.returncode, 0, independent.stderr)
        blocker.unlink()
        first.communicate(timeout=5)

    def test_branch_changes_are_recorded_and_surfaced(self) -> None:
        result = self.run_ralph(env={"FAKE_BRANCH_CHANGE": "agent-branch"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("branch changed from main to agent-branch", result.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        self.assertIn("agent-branch", (run_dir / "git-status-final.txt").read_text())

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

    def test_preflight_rejects_api_auth_without_starting_session_or_leaking_secret(self) -> None:
        secret = "sk-secret-value"
        result = self.run_ralph(env={"OPENAI_API_KEY": secret})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("API credential", result.stderr)
        self.assertNotIn(secret, result.stdout + result.stderr)
        opencode_calls = self.calls / "opencode"
        self.assertFalse(opencode_calls.exists() and " run " in opencode_calls.read_text())

    def test_preflight_rejects_unsafe_effective_config_and_model_mismatch(self) -> None:
        unsafe = json.loads(self._config())
        unsafe["provider"]["openai"]["options"]["baseURL"] = "https://proxy.invalid"
        config_result = self.run_ralph(env={"FAKE_CONFIG": json.dumps(unsafe)})
        self.assertNotEqual(config_result.returncode, 0)
        self.assertIn("effective OpenCode configuration", config_result.stderr)

        for path in self.calls.iterdir():
            path.unlink()
        mismatch_result = self.run_ralph(
            env={"FAKE_EVENTS": self._events("Done"), "FAKE_EXPORT": self._export("Done", "gpt-other")}
        )
        self.assertNotEqual(mismatch_result.returncode, 0)
        self.assertIn("initial model", mismatch_result.stderr)

    def test_prompt_and_model_validation_happen_before_session(self) -> None:
        self.prompt.write_bytes(b"\xff")
        invalid_prompt = self.run_ralph()
        self.assertNotEqual(invalid_prompt.returncode, 0)
        self.assertIn("UTF-8", invalid_prompt.stderr)

        self.prompt.write_text("work", encoding="utf-8")
        invalid_model = self.run_ralph("--model", "anthropic/claude")
        self.assertNotEqual(invalid_model.returncode, 0)
        self.assertIn("openai/", invalid_model.stderr)

    def test_preflight_rejects_backend_and_github_failures(self) -> None:
        cases = [
            ({"FAKE_VERSION": "1.17.19"}, "1.17.20"),
            ({"FAKE_MODELS": "openai/gpt-other"}, "unavailable"),
            ({"FAKE_AUTH": "OpenAI oauth\nAnthropic api"}, "OpenAI OAuth"),
            ({"FAKE_GH_FAIL": "1"}, "gh preflight"),
        ]
        for environment, message in cases:
            with self.subTest(environment=environment):
                result = self.run_ralph(env=environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                for path in self.calls.iterdir():
                    path.unlink()

    def test_claude_completion_uses_subscription_safe_headless_mode(self) -> None:
        result = self.run_ralph(backend="claude")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Work complete.", result.stdout)
        invocation = (self.calls / "claude").read_text()
        self.assertIn("-p --input-format stream-json --output-format stream-json", invocation)
        self.assertIn("--dangerously-skip-permissions", invocation)
        self.assertIn("--model claude-opus-4-8", invocation)
        self.assertIn("--setting-sources project --strict-mcp-config", invocation)
        self.assertNotIn("--bare", invocation)
        child_env = (self.calls / "claude-env").read_text()
        self.assertIn("DISABLE_AUTOUPDATER=1", child_env)
        self.assertIn("BASH_MAX_TIMEOUT_MS=2147483647", child_env)
        auth_env = (self.calls / "claude-auth-env").read_text()
        self.assertNotIn("ANTHROPIC_API_KEY=", auth_env)
        self.assertNotIn("ANTHROPIC_CUSTOM_HEADERS=", auth_env)
        composed = json.loads((self.calls / "claude-stdin").read_text())
        self.assertIn("Implement the selected issue.", composed["message"]["content"])
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        session = json.loads((run_dir / "session.json").read_text())
        self.assertEqual(session["session_id"], "claude-session-1")
        self.assertEqual(session["initial_model"], "claude-opus-4-8")
        self.assertEqual(session["fallback_models"], [])
        self.assertIn("claude diagnostic", (run_dir / "stderr.log").read_text())

    def test_claude_accepts_explicit_model_and_records_transient_fallback(self) -> None:
        requested = "claude-sonnet-4-6"
        events = self._claude_events("Implemented.", model=requested)
        assistant = json.loads(events.splitlines()[1])
        assistant["message"]["model"] = "claude-sonnet-4-5"
        event_lines = events.splitlines()
        event_lines[1] = json.dumps(assistant)

        result = self.run_ralph(
            "--model",
            requested,
            backend="claude",
            env={"FAKE_CLAUDE_EVENTS": "\n".join(event_lines)},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("iteration budget exhausted", result.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        session = json.loads((run_dir / "session.json").read_text())
        self.assertEqual(session["fallback_models"], ["claude-sonnet-4-5"])

    def test_claude_rejects_unsafe_auth_version_and_initial_model(self) -> None:
        cases = [
            ({"FAKE_CLAUDE_VERSION": "2.1.207"}, "2.1.208"),
            (
                {
                    "FAKE_CLAUDE_AUTH": json.dumps(
                        {"loggedIn": True, "authMethod": "console", "apiProvider": "firstParty"}
                    )
                },
                "subscription OAuth",
            ),
            (
                {
                    "CLAUDE_CODE_OAUTH_TOKEN": "team-token",
                    "FAKE_CLAUDE_AUTH": json.dumps(
                        {
                            "loggedIn": True,
                            "authMethod": "claude.ai",
                            "apiProvider": "firstParty",
                            "subscriptionType": "team",
                        }
                    )
                },
                "subscription OAuth",
            ),
            (
                {"FAKE_CLAUDE_EVENTS": self._claude_events("Done", model="claude-sonnet-4-6")},
                "initial model",
            ),
        ]
        for environment, message in cases:
            with self.subTest(environment=environment):
                result = self.run_ralph(backend="claude", env=environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                for path in self.calls.iterdir():
                    path.unlink()

    def test_claude_rejects_customizations_and_malformed_streams(self) -> None:
        agents = self.repo / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "unsafe.md").write_text("custom agent", encoding="utf-8")
        customized = self.run_ralph(backend="claude")
        self.assertNotEqual(customized.returncode, 0)
        self.assertIn("Claude customizations", customized.stderr)
        self.assertFalse((self.calls / "claude").exists())

        (agents / "unsafe.md").unlink()
        agents.rmdir()
        settings = self.repo / ".claude" / "settings.json"
        settings.write_text(json.dumps({"apiKeyHelper": "paid-key-command"}), encoding="utf-8")
        helper = self.run_ralph(backend="claude")
        self.assertNotEqual(helper.returncode, 0)
        self.assertIn("Claude customizations", helper.stderr)

        settings.unlink()
        (self.repo / ".claude").rmdir()
        malformed = self.run_ralph(backend="claude", env={"FAKE_CLAUDE_EVENTS": "not-json"})
        self.assertNotEqual(malformed.returncode, 0)
        self.assertIn("malformed structured output", malformed.stderr)

    def test_claude_oauth_token_is_preserved_but_api_credentials_are_rejected(self) -> None:
        token_result = self.run_ralph(
            backend="claude",
            env={"CLAUDE_CODE_OAUTH_TOKEN": "subscription-token"},
        )
        self.assertEqual(token_result.returncode, 0, token_result.stderr)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN=subscription-token", (self.calls / "claude-env").read_text())

        for path in self.calls.iterdir():
            path.unlink()
        api_result = self.run_ralph(backend="claude", env={"ANTHROPIC_AUTH_TOKEN": "paid-token"})
        self.assertNotEqual(api_result.returncode, 0)
        self.assertIn("API credential", api_result.stderr)
        self.assertNotIn("paid-token", api_result.stdout + api_result.stderr)

        for name in (
            "ANTHROPIC_AWS_BASE_URL",
            "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
            "ANTHROPIC_CUSTOM_HEADERS",
            "ANTHROPIC_FOUNDRY_API_KEY",
            "AWS_BEARER_TOKEN_BEDROCK",
            "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
            "CLAUDE_CODE_SKIP_VERTEX_AUTH",
            "CLAUDE_CODE_USE_ANTHROPIC_AWS",
            "CLAUDE_CODE_USE_MANTLE",
        ):
            with self.subTest(name=name):
                unsafe = self.run_ralph(backend="claude", env={name: "unsafe-routing"})
                self.assertNotEqual(unsafe.returncode, 0)
                self.assertIn("API credential", unsafe.stderr)

    def test_claude_rejects_cached_server_managed_settings(self) -> None:
        managed = self.base / ".claude" / "remote-settings.json"
        managed.parent.mkdir()
        managed.write_text(json.dumps({"hooks": {}}), encoding="utf-8")

        result = self.run_ralph(backend="claude", env={"HOME": str(self.base)})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("server-managed Claude settings", result.stderr)

    def test_claude_fails_closed_on_runtime_customization_and_backend_contract_errors(self) -> None:
        event_lines = self._claude_events("Done").splitlines()
        init = json.loads(event_lines[0])
        init["plugins"] = [{"name": "external-plugin"}]
        event_lines[0] = json.dumps(init)
        customized = self.run_ralph(
            backend="claude", env={"FAKE_CLAUDE_EVENTS": "\n".join(event_lines)}
        )
        self.assertNotEqual(customized.returncode, 0)
        self.assertIn("external MCP servers or plugins", customized.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        self.assertEqual(
            json.loads((run_dir / "session.json").read_text())["session_id"],
            "claude-session-1",
        )

        for path in self.calls.iterdir():
            path.unlink()
        missing_result = self.run_ralph(
            backend="claude",
            env={"FAKE_CLAUDE_EVENTS": "\n".join(self._claude_events("Done").splitlines()[:-1])},
        )
        self.assertNotEqual(missing_result.returncode, 0)
        self.assertIn("omitted required session metadata or final result", missing_result.stderr)

        for path in self.calls.iterdir():
            path.unlink()
        failed = self.run_ralph(backend="claude", env={"FAKE_CLAUDE_EXIT": "1"})
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("Claude session failed", failed.stderr)

    def test_isolated_package_install_exposes_cli_help_without_a_backend(self) -> None:
        backend = subprocess.run(
            [sys.executable, "-c", "import setuptools"],
            text=True,
            capture_output=True,
        )
        if backend.returncode:
            self.skipTest("setuptools build backend is not installed")
        target = self.base / "installed"
        installed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-build-isolation",
                "--no-deps",
                "--target",
                str(target),
                str(ROOT),
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)

        executable = target / "bin" / "ralph"
        help_result = subprocess.run(
            [str(executable), "--help"],
            env={**os.environ, "PYTHONPATH": str(target)},
            text=True,
            capture_output=True,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("{run,clean}", help_result.stdout)
        for command in ("run", "clean"):
            command_help = subprocess.run(
                [str(executable), command, "--help"],
                env={**os.environ, "PYTHONPATH": str(target)},
                text=True,
                capture_output=True,
            )
            self.assertEqual(command_help.returncode, 0, command_help.stderr)


if __name__ == "__main__":
    unittest.main()
