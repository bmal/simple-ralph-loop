from __future__ import annotations

import json
import os
from pathlib import Path
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
            }
        )
        if env:
            child_env.update(env)
        return child_env

    def _command(
        self, command: str = "run", *extra: str, worktree: Path | None = None
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
            "opencode",
            "--iterations",
            "1",
            "--worktree",
            str(selected_worktree),
            *extra,
        ]

    def run_ralph(self, *extra: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self._command("run", *extra),
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
        self.assertIn("INCOMPLETE", ambiguous_result.stderr)

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


if __name__ == "__main__":
    unittest.main()
