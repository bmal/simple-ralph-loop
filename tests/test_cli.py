from __future__ import annotations

import email
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import ralph  # noqa: E402  (import after sys.path is extended)
from ralph import cli  # noqa: E402  (import after sys.path is extended)


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
        # Isolated stand-ins for host state so Claude customization, routing, and
        # home-directory checks can never pass or fail because of the real
        # machine the suite happens to run on (managed profiles, MDM Claude
        # configuration, or the operator's home directory).
        self.home = self.base / "home"
        self.home.mkdir()
        self.managed_root = self.base / "managed-claude"
        self._write_fakes()

    def _script(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/sh\nset -eu\n" + textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o755)

    def _write_fakes(self) -> None:
        # Stand-in for `/usr/bin/profiles`: reports no managed configuration
        # profiles so the Claude managed-preferences check is deterministic and
        # never reads the host's real MDM state.
        self._script(
            "profiles",
            """
            printf '%s\\n' "$*" >> "$FAKE_CALLS/profiles"
            printf '%s\\n' 'There are no configuration profiles installed'
            """,
        )
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
              if test -n "${FAKE_CAFFEINATE_DIE:-}"; then
                # Survive the startup probe, then exit unexpectedly to model a
                # loop-wide assertion that is lost mid-run.
                sleep "$FAKE_CAFFEINATE_DIE"
                exit 0
              fi
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
              "--pure auth list")
                auth_count_file="$FAKE_CALLS/auth-count"
                auth_count=0
                test ! -f "$auth_count_file" || auth_count=$(cat "$auth_count_file")
                auth_count=$((auth_count + 1))
                printf '%s\\n' "$auth_count" > "$auth_count_file"
                if test -n "${FAKE_AUTH_MUTATED_FILE:-}" && test -e "$FAKE_AUTH_MUTATED_FILE"; then
                  printf '%s\\n' '┌ Credentials ~/.local/share/opencode/auth.json' '│' '● OpenAI oauth' '● Anthropic api' '│' '└ 2 credentials'
                else
                  printf '%s\\n' "${FAKE_AUTH}"
                fi
                ;;
              "--pure debug config") printf '%s\\n' "${FAKE_CONFIG}" ;;
              "--pure models openai") printf '%s\\n' "${FAKE_MODELS:-openai/gpt-5.6-sol}" ;;
              "--pure export "*)
                if test -n "${FAKE_RAW_EXPORT_FILE:-}"; then
                  cat "$FAKE_RAW_EXPORT_FILE"
                  exit 0
                fi
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
                if test -n "${FAKE_RAW_STDOUT_FILE:-}"; then
                  cat "$FAKE_RAW_STDOUT_FILE"
                  exit 0
                fi
                if test -n "${FAKE_ORPHAN_SLEEP:-}"; then
                  # A descendant keeps the stdout/stderr pipes open after the
                  # group leader exits, modelling a departed leader.
                  (sleep "$FAKE_ORPHAN_SLEEP") &
                  exit 0
                fi
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
                if test -n "${FAKE_AUTH_MUTATED_FILE:-}"; then
                  : > "$FAKE_AUTH_MUTATED_FILE"
                fi
                if test -n "${FAKE_BRANCH_CHANGE:-}"; then
                  git checkout -b "$FAKE_BRANCH_CHANGE" >/dev/null 2>&1
                fi
                if test -n "${FAKE_RAW_STDERR_FILE:-}"; then
                  cat "$FAKE_RAW_STDERR_FILE" >&2
                fi
                printf '%s\\n' "backend diagnostic" >&2
                exit "${FAKE_EXIT:-0}"
                ;;
              *"--session "*)
                printf '%s\\n' "$*" >> "$FAKE_CALLS/opencode-resume"
                env | sort > "$FAKE_CALLS/opencode-resume-env"
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
                auth_count_file="$FAKE_CALLS/claude-auth-count"
                auth_count=0
                test ! -f "$auth_count_file" || auth_count=$(cat "$auth_count_file")
                auth_count=$((auth_count + 1))
                printf '%s\n' "$auth_count" > "$auth_count_file"
                env | sort > "$FAKE_CALLS/claude-auth-env"
                printf '%s\n' "${FAKE_CLAUDE_AUTH}"
                ;;
              "-p "*)
                cat > "$FAKE_CALLS/claude-stdin"
                env | sort > "$FAKE_CALLS/claude-env"
                printf '%s\n' "${FAKE_CLAUDE_EVENTS}"
                if test -n "${FAKE_CLAUDE_RAW_STDOUT_FILE:-}"; then
                  cat "$FAKE_CLAUDE_RAW_STDOUT_FILE"
                  exit 0
                fi
                if test -n "${FAKE_CLAUDE_ORPHAN_SLEEP:-}"; then
                  (sleep "$FAKE_CLAUDE_ORPHAN_SLEEP") &
                  exit 0
                fi
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
                if test -n "${FAKE_CLAUDE_MUTATE_CUSTOMIZATION:-}"; then
                  mkdir -p "$FAKE_CLAUDE_MUTATE_CUSTOMIZATION"
                fi
                if test -n "${FAKE_CLAUDE_RAW_STDERR_FILE:-}"; then
                  cat "$FAKE_CLAUDE_RAW_STDERR_FILE" >&2
                fi
                if test -n "${FAKE_CLAUDE_LEAK_STDERR:-}"; then
                  printf 'diagnostic token %s here\n' "${CLAUDE_CODE_OAUTH_TOKEN:-}" >&2
                else
                  printf '%s\n' "claude diagnostic" >&2
                fi
                exit "${FAKE_CLAUDE_EXIT:-0}"
                ;;
              "--resume "*)
                printf '%s\n' "$*" >> "$FAKE_CALLS/claude-resume"
                env | sort > "$FAKE_CALLS/claude-resume-env"
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

    def _export_messages(
        self,
        text: str,
        models: list[tuple[str, str]],
        session_id: str = "ses_1",
    ) -> str:
        messages = []
        for index, (provider, model) in enumerate(models, 1):
            messages.append(
                {
                    "info": {
                        "id": f"msg_{index}",
                        "sessionID": session_id,
                        "role": "assistant",
                        "providerID": provider,
                        "modelID": model,
                    },
                    "parts": [{"id": f"part_{index}", "type": "text", "text": text}],
                }
            )
        return json.dumps({"info": {"id": session_id}, "messages": messages})

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

    # Host variables the child legitimately needs (locale, temp directories,
    # certificate discovery, and git's idea of the current user). Anything not
    # listed here — notably operator LLM API keys, custom endpoints, and ambient
    # Claude session variables such as ANTHROPIC_BASE_URL or CLAUDE_CODE_* — is
    # deliberately dropped so the suite behaves identically regardless of the
    # shell it runs in and never trips Ralph's fail-closed environment checks.
    _ENV_ALLOWLIST = (
        "PATH",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "TERM",
        "TMPDIR",
        "TEMP",
        "TMP",
        "USER",
        "LOGNAME",
        "SHELL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    )

    def _environment(self, env: dict[str, str] | None = None) -> dict[str, str]:
        child_env = {
            key: os.environ[key] for key in self._ENV_ALLOWLIST if key in os.environ
        }
        child_env.update(
            {
                "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
                "PYTHONPATH": str(ROOT / "src"),
                # Absolute path to the fake caffeinate: production uses
                # /usr/bin/caffeinate, and this test-only seam substitutes it.
                "RALPH_CAFFEINATE": str(self.bin / "caffeinate"),
                # Redirect every host-state lookup at isolated stand-ins so
                # managed-configuration and home-directory checks are
                # deterministic (see setUp).
                "HOME": str(self.home),
                "RALPH_CLAUDE_MANAGED_ROOT": str(self.managed_root),
                "RALPH_CLAUDE_PROFILES": str(self.bin / "profiles"),
                "FAKE_CALLS": str(self.calls),
                "FAKE_CONFIG": self._config(),
                "FAKE_AUTH": "┌  Credentials ~/.local/share/opencode/auth.json\n│\n●  OpenAI oauth\n│\n└  1 credentials",
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

    def resume_ralph(
        self,
        backend: str,
        model: str,
        session: str,
        *extra: str,
        env: dict[str, str] | None = None,
        worktree: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "ralph.cli",
                "resume",
                "--backend",
                backend,
                "--model",
                model,
                "--worktree",
                str(worktree or self.repo),
                "--session",
                session,
                *extra,
            ],
            cwd=self.base,
            env=self._environment(env),
            text=True,
            capture_output=True,
        )

    def _await_ready(
        self,
        marker: Path,
        process: subprocess.Popen[str],
        *,
        what: str = "backend",
        timeout: float = 20.0,
    ) -> None:
        """Block until *marker* exists, using an explicit deadline instead of a
        fixed polling window so a slow machine gets ample time. If the child
        exits before signalling readiness, fail immediately with its captured
        output rather than waiting out the timeout on a process that is already
        gone."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if marker.exists():
                return
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(
                    f"{what} exited early with status {process.returncode} before "
                    f"signalling readiness:\n{stdout}{stderr}"
                )
            time.sleep(0.01)
        self.fail(f"{what} did not signal readiness within {timeout:.0f}s")

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
        self.assertIn("ralph resume --backend claude", result.stderr)
        self.assertIn("--session claude-session-1", result.stderr)
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

    def _invalid_utf8_file(self, name: str, prefix: bytes = b"") -> Path:
        path = self.base / name
        # 0xFF is never valid in a UTF-8 stream, so a strict decoder must fail.
        path.write_bytes(prefix + b"\xff\xfe not utf-8\n")
        return path

    def _run_guarded(
        self,
        *extra: str,
        env: dict[str, str] | None = None,
        backend: str = "opencode",
        timeout: float = 20,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                self._command("run", *extra, backend=backend),
                cwd=self.base,
                env=self._environment(env),
                text=True,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as expired:
            self.fail(
                "ralph blocked instead of terminating the backend process group: "
                f"{expired}"
            )

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

    def _run_backend_question(self, backend: str, text: str) -> subprocess.CompletedProcess[str]:
        if backend == "claude":
            return self.run_ralph(
                backend="claude", env={"FAKE_CLAUDE_EVENTS": self._claude_events(text)}
            )
        return self.run_ralph(
            env={"FAKE_EVENTS": self._events(text), "FAKE_EXPORT": self._export(text)}
        )

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
        self._await_ready(Path(f"{blocker}.ready"), first, what="first loop")

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
        self._await_ready(Path(f"{blocker}.ready"), first, what="first worktree")

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

    def _add_linked_worktree(self, name: str) -> Path:
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
        other = self.base / name
        subprocess.run(
            ["git", "worktree", "add", "-b", name, str(other)],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        return other

    def test_linked_worktrees_keep_independent_state(self) -> None:
        other = self._add_linked_worktree("second")
        main_run = self.run_ralph()
        self.assertEqual(main_run.returncode, 0, main_run.stderr)
        for path in self.calls.iterdir():
            path.unlink()
        other_run = subprocess.run(
            self._command(worktree=other),
            cwd=self.base,
            env=self._environment(),
            text=True,
            capture_output=True,
        )
        self.assertEqual(other_run.returncode, 0, other_run.stderr)

        main_runs = self.repo / ".git" / "ralph" / "runs"
        linked_runs = self.repo / ".git" / "worktrees" / "second" / "ralph" / "runs"
        self.assertTrue(any(main_runs.iterdir()))
        self.assertTrue(linked_runs.is_dir() and any(linked_runs.iterdir()))

    def test_run_refuses_symlinked_ralph_state_directory(self) -> None:
        outside = self.base / "outside-state"
        outside.mkdir()
        os.symlink(outside, self.repo / ".git" / "ralph")

        result = self.run_ralph()

        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("symlink", result.stderr)
        # Nothing was redirected outside the resolved Git directory.
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.calls / "opencode").exists())

    def test_run_refuses_non_directory_ralph_state(self) -> None:
        (self.repo / ".git" / "ralph").write_text("not a directory", encoding="utf-8")

        result = self.run_ralph()

        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("not a directory", result.stderr)

    def test_run_refuses_symlinked_runs_subdirectory(self) -> None:
        (self.repo / ".git" / "ralph").mkdir()
        outside = self.base / "outside-runs"
        outside.mkdir()
        os.symlink(outside, self.repo / ".git" / "ralph" / "runs")

        result = self.run_ralph()

        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("symlink", result.stderr)
        self.assertEqual(list(outside.iterdir()), [])

    def test_clean_refuses_symlinked_state_and_preserves_target(self) -> None:
        outside = self.base / "outside-clean"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep", encoding="utf-8")
        os.symlink(outside, self.repo / ".git" / "ralph")

        result = self.clean_ralph()

        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("symlink", result.stderr)
        self.assertTrue((outside / "keep.txt").exists())

    def test_clean_removes_state_without_following_symlinked_children(self) -> None:
        self.assertEqual(self.run_ralph().returncode, 0)
        outside = self.base / "outside-child"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep", encoding="utf-8")
        os.symlink(outside, self.repo / ".git" / "ralph" / "link-to-outside")

        cleaned = self.clean_ralph()

        self.assertEqual(cleaned.returncode, 0, cleaned.stderr)
        self.assertFalse((self.repo / ".git" / "ralph").exists())
        # The symlink target and its contents were never followed or deleted.
        self.assertTrue((outside / "keep.txt").exists())

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
        over = self.run_ralph("--timeout", str(cli.MAX_ITERATION_TIMEOUT_SECONDS + 1))
        self.assertNotEqual(over.returncode, 0)
        self.assertIn("must not exceed", over.stderr)
        self.assertFalse((self.calls / "opencode").exists())

        at_max = self.run_ralph("--timeout", str(cli.MAX_ITERATION_TIMEOUT_SECONDS))
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

    def test_opencode_auth_output_contract_is_strict(self) -> None:
        supported = "┌  Credentials ~/.local/share/opencode/auth.json\n│\n●  OpenAI oauth\n│\n└  1 credentials"
        accepted = self.run_ralph(env={"FAKE_AUTH": supported})
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

        for auth in (
            "OpenAI OAuth token",
            "┌ Credentials path\n│\n● OpenAI oauth\n● Unknown credential\n│\n└ 2 credentials",
            "┌ Credentials ~/.local/share/opencode/auth.json\n│\n● OpenAI oauth\n│\n└ 2 credentials",
        ):
            with self.subTest(auth=auth):
                for path in self.calls.iterdir():
                    path.unlink()
                rejected = self.run_ralph(env={"FAKE_AUTH": auth})
                self.assertEqual(rejected.returncode, 2)
                self.assertIn("unfamiliar or ambiguous", rejected.stderr)
                calls = (self.calls / "opencode").read_text()
                self.assertNotIn(" run ", calls)

    def test_opencode_validates_every_exported_assistant_route_and_records_fallback(self) -> None:
        alternate = self._export_messages(
            "Done",
            [("openai", "gpt-5.6-sol"), ("anthropic", "claude-opus-4-8")],
        )
        rejected = self.run_ralph(env={"FAKE_EXPORT": alternate})
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("session export omitted required metadata", rejected.stderr)

        for path in self.calls.iterdir():
            path.unlink()
        fallback_export = self._export_messages(
            "Implemented.",
            [("openai", "gpt-5.6-sol"), ("openai", "gpt-5.5-codex")],
        )
        fallback = self.run_ralph(
            env={
                "FAKE_EVENTS": self._events("Implemented."),
                "FAKE_EXPORT": fallback_export,
            }
        )
        self.assertEqual(fallback.returncode, 1, fallback.stderr)
        runs = sorted((self.repo / ".git" / "ralph" / "runs").iterdir())
        session = json.loads((runs[-1] / "session.json").read_text())
        self.assertEqual(
            session["ralph_verification"]["fallback_models"],
            ["openai/gpt-5.5-codex"],
        )

    def test_opencode_rejects_later_streamed_provider_substitution(self) -> None:
        events = [
            {
                "type": "message.updated",
                "properties": {
                    "info": {
                        "id": "msg_1",
                        "sessionID": "ses_1",
                        "role": "assistant",
                        "providerID": "openai",
                        "modelID": "gpt-5.6-sol",
                    }
                },
            },
            {
                "type": "message.updated",
                "properties": {
                    "info": {
                        "id": "msg_2",
                        "sessionID": "ses_1",
                        "role": "assistant",
                        "providerID": "anthropic",
                        "modelID": "claude-opus-4-8",
                    }
                },
            },
        ]
        result = self.run_ralph(env={"FAKE_EVENTS": "\n".join(map(json.dumps, events))})
        self.assertEqual(result.returncode, 2)
        self.assertIn("alternate or malformed provider route", result.stderr)
        self.assertIn("--session ses_1", result.stderr)

        for path in self.calls.iterdir():
            path.unlink()
        missing_session = events[0].copy()
        missing_session["properties"] = {"info": dict(events[0]["properties"]["info"])}
        del missing_session["properties"]["info"]["sessionID"]
        missing = self.run_ralph(env={"FAKE_EVENTS": json.dumps(missing_session)})
        self.assertEqual(missing.returncode, 2)
        self.assertIn("omitted routing metadata", missing.stderr)
        run_dirs = sorted((self.repo / ".git" / "ralph" / "runs").iterdir())
        outcome = json.loads((run_dirs[-1] / "outcome.json").read_text())
        self.assertEqual(outcome["outcome"], "backend_contract_failure")
        self.assertEqual(len(outcome["iterations"]), 1)

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

    def test_unsafe_allow_claude_agents_relaxes_only_the_agent_vectors(self) -> None:
        agents = self.repo / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "custom.md").write_text("custom agent", encoding="utf-8")

        refused = self.run_ralph(backend="claude")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("Claude customizations", refused.stderr)
        self.assertFalse((self.calls / "claude").exists())

        allowed = self.run_ralph("--unsafe-allow-claude-agents", backend="claude")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertTrue((self.calls / "claude").exists())
        self.assertIn("--unsafe-allow-claude-agents is set", allowed.stderr)

        for path in self.calls.iterdir():
            path.unlink()

        # The flag is scoped to agents: a co-present hooks directory is still
        # refused, and the backend is never launched.
        hooks = self.repo / ".claude" / "hooks"
        hooks.mkdir()
        with_hooks = self.run_ralph("--unsafe-allow-claude-agents", backend="claude")
        self.assertNotEqual(with_hooks.returncode, 0)
        self.assertIn("Claude customizations", with_hooks.stderr)
        self.assertFalse((self.calls / "claude").exists())
        hooks.rmdir()

        # settings.json: the flag admits the `agent` key but not other unsafe keys.
        settings = self.repo / ".claude" / "settings.json"
        settings.write_text(json.dumps({"agent": {"reviewer": {}}}), encoding="utf-8")
        agent_key = self.run_ralph("--unsafe-allow-claude-agents", backend="claude")
        self.assertEqual(agent_key.returncode, 0, agent_key.stderr)

        for path in self.calls.iterdir():
            path.unlink()
        settings.write_text(
            json.dumps({"agent": {"reviewer": {}}, "hooks": {}}), encoding="utf-8"
        )
        mixed = self.run_ralph("--unsafe-allow-claude-agents", backend="claude")
        self.assertNotEqual(mixed.returncode, 0)
        self.assertIn("Claude customizations", mixed.stderr)

    def test_agent_only_refusal_advertises_the_opt_out(self) -> None:
        claude_dir = self.repo / ".claude"
        claude_dir.mkdir()
        settings = claude_dir / "settings.json"

        # An agents-directory-only refusal names the opt-out.
        agents = claude_dir / "agents"
        agents.mkdir()
        (agents / "custom.md").write_text("agent", encoding="utf-8")
        dir_only = self.run_ralph(backend="claude")
        self.assertNotEqual(dir_only.returncode, 0)
        self.assertIn("Claude customizations", dir_only.stderr)
        self.assertIn("--unsafe-allow-claude-agents", dir_only.stderr)
        self.assertFalse((self.calls / "claude").exists())
        (agents / "custom.md").unlink()
        agents.rmdir()

        # An `agent`-key-only refusal names the opt-out too.
        settings.write_text(json.dumps({"agent": {"reviewer": {}}}), encoding="utf-8")
        key_only = self.run_ralph(backend="claude")
        self.assertNotEqual(key_only.returncode, 0)
        self.assertIn("Claude customizations", key_only.stderr)
        self.assertIn("--unsafe-allow-claude-agents", key_only.stderr)
        settings.unlink()

    def test_non_agent_refusals_never_advertise_the_opt_out(self) -> None:
        claude_dir = self.repo / ".claude"
        claude_dir.mkdir()
        settings = claude_dir / "settings.json"
        agents = claude_dir / "agents"

        def _refusal(*, agents_present: bool, dir_name: str | None, keys: dict) -> str:
            if agents_present:
                agents.mkdir()
            other_dir = claude_dir / dir_name if dir_name else None
            if other_dir is not None:
                other_dir.mkdir()
            if keys:
                settings.write_text(json.dumps(keys), encoding="utf-8")
            result = self.run_ralph(backend="claude")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Claude customizations", result.stderr)
            # The opt-out must never be dangled when the flag cannot relax the
            # blocker; setting it would be a false remedy.
            self.assertNotIn("--unsafe-allow-claude-agents", result.stderr)
            self.assertFalse((self.calls / "claude").exists())
            if agents_present:
                agents.rmdir()
            if other_dir is not None:
                other_dir.rmdir()
            if settings.exists():
                settings.unlink()
            return result.stderr

        # A hooks directory and a plugins directory each stay plain.
        _refusal(agents_present=False, dir_name="hooks", keys={})
        _refusal(agents_present=False, dir_name="plugins", keys={})
        # A mixed agents+hooks layout stays plain (agents is not the sole blocker).
        _refusal(agents_present=True, dir_name="hooks", keys={})
        _refusal(agents_present=True, dir_name="plugins", keys={})
        # Another unsafe key alone stays plain.
        _refusal(agents_present=False, dir_name=None, keys={"hooks": {}})
        _refusal(agents_present=False, dir_name=None, keys={"env": {"X": "1"}})
        # `agent` alongside another unsafe key stays plain.
        _refusal(agents_present=False, dir_name=None, keys={"agent": {}, "hooks": {}})
        # The agents directory alongside a non-agent settings key stays plain.
        _refusal(agents_present=True, dir_name=None, keys={"hooks": {}})

    def test_managed_config_refusal_never_advertises_the_opt_out(self) -> None:
        # Managed configuration is refused even when an agents directory is the
        # only local customization: the flag cannot relax managed config, so the
        # refusal must stay plain and take precedence over the agent vector.
        agents = self.repo / ".claude" / "agents"
        agents.mkdir(parents=True)
        self.managed_root.mkdir()
        (self.managed_root / "managed-settings.json").write_text("{}", encoding="utf-8")

        result = self.run_ralph(backend="claude")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("managed Claude configuration", result.stderr)
        self.assertNotIn("--unsafe-allow-claude-agents", result.stderr)
        self.assertFalse((self.calls / "claude").exists())

    def test_flag_is_backend_strict_on_run_and_resume(self) -> None:
        # The flag is Claude-specific; combining it with OpenCode is refused
        # fail-closed before any backend, preflight, or handoff command exists.
        run_opencode = self.run_ralph(
            "--unsafe-allow-claude-agents", backend="opencode"
        )
        self.assertNotEqual(run_opencode.returncode, 0)
        self.assertIn("--unsafe-allow-claude-agents is only valid with --backend claude", run_opencode.stderr)
        self.assertFalse((self.calls / "opencode").exists())
        self.assertFalse((self.calls / "caffeinate").exists())

        resume_opencode = self.resume_ralph(
            "opencode", "openai/gpt-5.6-sol", "ses_1", "--unsafe-allow-claude-agents"
        )
        self.assertNotEqual(resume_opencode.returncode, 0)
        self.assertIn("--unsafe-allow-claude-agents is only valid with --backend claude", resume_opencode.stderr)
        self.assertFalse((self.calls / "opencode").exists())
        self.assertFalse((self.calls / "caffeinate").exists())

    def test_claude_handoff_reproduces_flag_with_session_last(self) -> None:
        events = self._claude_events("unused").splitlines()
        assistant = json.loads(events[1])
        assistant["message"]["content"] = [
            {
                "type": "tool_use",
                "name": "AskUserQuestion",
                "input": {"questions": [{"question": "Which migration path should I take?"}]},
            }
        ]
        claude_events = "\n".join([events[0], json.dumps(assistant)])
        agents = self.repo / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "custom.md").write_text("agent", encoding="utf-8")

        with_flag = self.run_ralph(
            "--unsafe-allow-claude-agents",
            "--iterations",
            "2",
            backend="claude",
            env={"FAKE_CLAUDE_EVENTS": claude_events},
        )
        self.assertEqual(with_flag.returncode, 2)
        # Both the resume and the run command reproduce the flag so recovery
        # re-establishes the same relaxed boundary.
        self.assertIn("ralph resume --backend claude", with_flag.stderr)
        resume_line = next(
            line for line in with_flag.stderr.splitlines() if "manual resume:" in line
        )
        self.assertIn("--unsafe-allow-claude-agents", resume_line)
        # --session must remain the final argument of the resume command.
        self.assertTrue(resume_line.rstrip().endswith("--session claude-session-1"))
        continue_line = next(
            line for line in with_flag.stderr.splitlines() if "continue Ralph:" in line
        )
        self.assertIn("--unsafe-allow-claude-agents", continue_line)

        for path in self.calls.iterdir():
            path.unlink()

        # Without the flag, neither command mentions it. (Move the agents dir
        # aside so the no-flag run is not refused before it can hand off.)
        (agents / "custom.md").unlink()
        agents.rmdir()
        without_flag = self.run_ralph(
            "--iterations",
            "2",
            backend="claude",
            env={"FAKE_CLAUDE_EVENTS": claude_events},
        )
        self.assertEqual(without_flag.returncode, 2)
        self.assertIn("manual resume:", without_flag.stderr)
        self.assertNotIn("--unsafe-allow-claude-agents", without_flag.stderr)

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

    def test_claude_rejects_managed_configuration_directory(self) -> None:
        # The managed-root check is host-isolated through a seam; confirm the seam
        # still fires (it is not a silent bypass) when managed config is present.
        self.managed_root.mkdir()
        (self.managed_root / "managed-settings.json").write_text("{}", encoding="utf-8")

        result = self.run_ralph(backend="claude")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("managed Claude configuration", result.stderr)

    def test_claude_rejects_managed_configuration_profiles(self) -> None:
        # A configuration profile that manages Claude Code must stop the run. The
        # profiles tool is host-isolated through a seam, so drive it with a fake
        # that reports a managing profile and confirm the check still fires.
        self._script(
            "profiles",
            """
            printf '%s\\n' "$*" >> "$FAKE_CALLS/profiles"
            printf '%s\\n' 'com.anthropic.claudecode'
            """,
        )

        result = self.run_ralph(backend="claude")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("managed Claude preferences", result.stderr)

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

    def test_resume_relaunches_sanitized_full_auto_backend(self) -> None:
        opencode = self.resume_ralph("opencode", "openai/gpt-5.6-sol", "ses_9")
        self.assertEqual(opencode.returncode, 0, opencode.stderr)
        resume_call = (self.calls / "opencode-resume").read_text()
        self.assertIn("--session ses_9", resume_call)
        self.assertIn("--auto", resume_call)
        self.assertIn("--model openai/gpt-5.6-sol", resume_call)
        self.assertIn(f"--dir {self.repo.resolve()}", resume_call)
        self.assertIn("-im", (self.calls / "caffeinate").read_text())
        resume_env = (self.calls / "opencode-resume-env").read_text()
        self.assertIn("OPENCODE_DISABLE_AUTOUPDATE=true", resume_env)
        self.assertIn("OPENCODE_CONFIG_CONTENT=", resume_env)
        self.assertIn("OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS=2147483647", resume_env)
        self.assertNotIn("OPENAI_API_KEY=", resume_env)
        # Effective routing/auth is re-proved before the interactive session.
        self.assertTrue((self.calls / "auth-count").exists())

        for path in self.calls.iterdir():
            path.unlink()
        claude = self.resume_ralph("claude", "claude-opus-4-8", "claude-session-1")
        self.assertEqual(claude.returncode, 0, claude.stderr)
        claude_call = (self.calls / "claude-resume").read_text()
        self.assertIn("--resume claude-session-1", claude_call)
        self.assertIn("--dangerously-skip-permissions", claude_call)
        self.assertIn("--model claude-opus-4-8", claude_call)
        self.assertIn("--setting-sources project --strict-mcp-config", claude_call)
        self.assertIn("-im", (self.calls / "caffeinate").read_text())
        claude_env = (self.calls / "claude-resume-env").read_text()
        self.assertIn("DISABLE_AUTOUPDATER=1", claude_env)
        self.assertIn("BASH_MAX_TIMEOUT_MS=2147483647", claude_env)
        self.assertNotIn("ANTHROPIC_API_KEY=", claude_env)
        self.assertTrue((self.calls / "claude-auth-count").exists())

    def test_resume_refuses_unsafe_recovery_environment(self) -> None:
        secret = "sk-live-secret-value"
        api = self.resume_ralph(
            "opencode", "openai/gpt-5.6-sol", "ses_1", env={"OPENAI_API_KEY": secret}
        )
        self.assertNotEqual(api.returncode, 0)
        self.assertIn("API credential", api.stderr)
        self.assertNotIn(secret, api.stdout + api.stderr)
        self.assertFalse((self.calls / "opencode-resume").exists())

        for path in self.calls.iterdir():
            path.unlink()
        changed = self.resume_ralph(
            "opencode",
            "openai/gpt-5.6-sol",
            "ses_1",
            env={"FAKE_AUTH": "OpenAI oauth\nAnthropic api"},
        )
        self.assertEqual(changed.returncode, 2)
        self.assertIn("unfamiliar or ambiguous", changed.stderr)
        self.assertFalse((self.calls / "opencode-resume").exists())

        for path in self.calls.iterdir():
            path.unlink()
        plugin_dir = self.repo / ".opencode" / "plugin"
        plugin_dir.mkdir(parents=True)
        plugin = self.resume_ralph("opencode", "openai/gpt-5.6-sol", "ses_1")
        self.assertNotEqual(plugin.returncode, 0)
        self.assertIn("external plugins or custom tools", plugin.stderr)
        self.assertFalse((self.calls / "opencode-resume").exists())
        plugin_dir.rmdir()
        (self.repo / ".opencode").rmdir()

        for path in self.calls.iterdir():
            path.unlink()
        settings = self.repo / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"apiKeyHelper": "paid-key-command"}), encoding="utf-8")
        customized = self.resume_ralph("claude", "claude-opus-4-8", "claude-session-1")
        self.assertNotEqual(customized.returncode, 0)
        self.assertIn("Claude customizations", customized.stderr)
        self.assertFalse((self.calls / "claude-resume").exists())

    def test_resume_rejects_provider_mismatched_model(self) -> None:
        result = self.resume_ralph("opencode", "anthropic/claude", "ses_1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("openai/", result.stderr)
        self.assertFalse((self.calls / "opencode-resume").exists())

    def test_streamed_oauth_token_is_redacted_from_claude_streams(self) -> None:
        token = "oauth-subscription-token-1234567890"
        text = (
            f"Token is {token} inside output.\n"
            "Work complete.\n<promise>COMPLETE</promise>"
        )
        result = self.run_ralph(
            backend="claude",
            env={
                "CLAUDE_CODE_OAUTH_TOKEN": token,
                "FAKE_CLAUDE_EVENTS": self._claude_events(text),
                "FAKE_CLAUDE_LEAK_STDERR": "1",
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(token, result.stdout)
        self.assertIn("Token is [redacted] inside output.", result.stdout)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        stdout_ndjson = (run_dir / "stdout.ndjson").read_text()
        self.assertNotIn(token, stdout_ndjson)
        self.assertIn("[redacted]", stdout_ndjson)
        stderr_log = (run_dir / "stderr.log").read_text()
        self.assertNotIn(token, stderr_log)
        self.assertIn("[redacted]", stderr_log)
        # The child session still receives the real credential.
        self.assertIn(
            f"CLAUDE_CODE_OAUTH_TOKEN={token}", (self.calls / "claude-env").read_text()
        )

    def test_oauth_token_redaction_keeps_json_export_parseable(self) -> None:
        token = "oauth-subscription-token-1234567890"
        text = f"Echoed {token} back.\nWork complete.\n<promise>COMPLETE</promise>"
        result = self.run_ralph(
            env={
                "CLAUDE_CODE_OAUTH_TOKEN": token,
                "FAKE_EVENTS": self._events(text),
                "FAKE_EXPORT": self._export(text),
            }
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        session_text = (run_dir / "session.json").read_text()
        self.assertNotIn(token, session_text)
        self.assertIn("[redacted]", session_text)
        # Redaction must not corrupt the retained structured export.
        json.loads(session_text)
        self.assertNotIn(token, (run_dir / "stdout.ndjson").read_text())

    def test_streamed_secret_split_across_chunks_is_not_leaked_to_console(self) -> None:
        # OpenCode streams a growing text part. The secret straddles the boundary
        # between what was already printed and the new suffix, so a naive raw-delta
        # redaction would print each half unredacted and the full token would
        # appear on stdout. Redacting the whole accumulated text must prevent that.
        token = "oauth-subscription-token-1234567890"
        first = f"Token: {token[:20]}"
        full = f"Token: {token} echoed.\nWork complete.\n<promise>COMPLETE</promise>"

        def text_event(text: str) -> str:
            return json.dumps(
                {
                    "type": "text",
                    "sessionID": "ses_1",
                    "part": {
                        "id": "part_1",
                        "sessionID": "ses_1",
                        "messageID": "msg_1",
                        "type": "text",
                        "text": text,
                        "time": {"start": 1, "end": 2},
                    },
                }
            )

        result = self.run_ralph(
            env={
                "CLAUDE_CODE_OAUTH_TOKEN": token,
                "FAKE_EVENTS": text_event(first) + "\n" + text_event(full),
                "FAKE_EXPORT": self._export(full),
            }
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(token, result.stdout)
        self.assertIn("[redacted]", result.stdout)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        self.assertNotIn(token, (run_dir / "stdout.ndjson").read_text())

    def test_deeply_nested_json_fails_closed_without_traceback(self) -> None:
        # JSON nested past the interpreter's recursion limit raises RecursionError
        # rather than json.JSONDecodeError. Both backends must treat it as
        # malformed structured output and hand off, never emit a raw traceback.
        deep = self.base / "deep.json"
        depth = 100_000
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

    def test_dirty_worktree_warns_but_permits_the_run(self) -> None:
        # A dirty worktree is recorded and warned about but never refused.
        (self.repo / "uncommitted.txt").write_text("work in progress", encoding="utf-8")

        result = self.run_ralph()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("uncommitted changes", result.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        self.assertIn("uncommitted.txt", (run_dir / "git-status.txt").read_text())

class PackagingLifecycleTest(unittest.TestCase):
    """Non-skippable, backend-free packaging qualification.

    The supported qualification path is expected to provide the declared PEP 517
    build backend (setuptools). Its absence is a hard failure here rather than a
    silent skip, so a broken qualification environment cannot masquerade as a
    passing packaging check. No live language model is ever invoked.
    """

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import setuptools  # noqa: F401
        except ImportError as error:  # pragma: no cover - only on a broken env
            raise AssertionError(
                "the declared PEP 517 build backend (setuptools>=77) is required "
                "for packaging qualification and must not be skipped"
            ) from error

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        # Build from an isolated copy of the packaging inputs so wheel/sdist
        # construction never writes build artifacts into the checkout under test.
        self.source = self.work / "source"
        self.source.mkdir()
        shutil.copytree(ROOT / "src", self.source / "src")
        for name in ("pyproject.toml", "README.md", "LICENSE"):
            shutil.copy2(ROOT / name, self.source / name)

    def _pep517_build(self, hook: str) -> Path:
        """Invoke the declared setuptools PEP 517 backend directly, using only
        declared build tooling and the standard library (no third-party build
        front-end)."""
        out = self.work / hook
        out.mkdir()
        # The backend logs build chatter to stdout; redirect it to stderr so the
        # only thing on stdout is the returned artifact name.
        script = (
            "import contextlib, sys\n"
            "from setuptools import build_meta\n"
            "with contextlib.redirect_stdout(sys.stderr):\n"
            f"    name = build_meta.{hook}(sys.argv[1])\n"
            "sys.stdout.write(name)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(out)],
            cwd=self.source,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        artifact = out / result.stdout.strip()
        self.assertTrue(artifact.is_file(), f"{hook} produced no artifact: {result.stdout!r}")
        return artifact

    def test_wheel_install_exposes_cli_help_without_a_backend(self) -> None:
        wheel = self._pep517_build("build_wheel")
        target = self.work / "install"
        installed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                "--target",
                str(target),
                str(wheel),
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)

        executable = target / "bin" / "ralph"
        self.assertTrue(executable.is_file(), "console entry point was not installed")
        env = {**os.environ, "PYTHONPATH": str(target)}
        top = subprocess.run(
            [str(executable), "--help"], env=env, text=True, capture_output=True
        )
        self.assertEqual(top.returncode, 0, top.stderr)
        self.assertIn("{run,clean,resume}", top.stdout)
        for sub in ("run", "clean", "resume"):
            sub_help = subprocess.run(
                [str(executable), sub, "--help"], env=env, text=True, capture_output=True
            )
            self.assertEqual(sub_help.returncode, 0, sub_help.stderr)
            self.assertIn(sub, sub_help.stdout)

    def test_wheel_metadata_declares_entry_point_version_license_and_no_deps(self) -> None:
        wheel = self._pep517_build("build_wheel")
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            metadata_text = archive.read(
                next(n for n in names if n.endswith(".dist-info/METADATA"))
            ).decode("utf-8")
            entry_points = archive.read(
                next(n for n in names if n.endswith(".dist-info/entry_points.txt"))
            ).decode("utf-8")
            license_members = [
                n
                for n in names
                if ".dist-info/licenses/" in n or n.endswith(".dist-info/LICENSE")
            ]
            license_text = archive.read(license_members[0]).decode("utf-8") if license_members else ""

        metadata = email.message_from_string(metadata_text)
        self.assertEqual(metadata["Name"], "simple-ralph-loop")
        self.assertEqual(metadata["Version"], "0.1.0")
        self.assertEqual(metadata["Version"], ralph.__version__)
        self.assertIn("3.11", metadata["Requires-Python"] or "")
        # Empty runtime dependency set: setuptools omits Requires-Dist entirely.
        self.assertIsNone(metadata.get_all("Requires-Dist"))
        # License attribution: SPDX expression plus the bundled license file.
        self.assertEqual(metadata["License-Expression"], "MIT")
        self.assertTrue(license_members, "LICENSE file was not bundled in the wheel")
        self.assertIn("MIT License", license_text)
        # Console entry point routes `ralph` to the CLI main.
        self.assertIn("[console_scripts]", entry_points)
        self.assertIn("ralph = ralph.cli:main", entry_points)
        # The corrected README must not resurface the unpublished-PyPI claim.
        self.assertNotIn("pipx install simple-ralph-loop", metadata_text)

    def test_sdist_contains_sources_and_metadata(self) -> None:
        sdist = self._pep517_build("build_sdist")
        self.assertTrue(sdist.name.endswith(".tar.gz"), sdist.name)
        with tarfile.open(sdist, "r:gz") as archive:
            members = archive.getnames()
            pkg_info_name = next(n for n in members if n.endswith("/PKG-INFO"))
            pkg_info = archive.extractfile(pkg_info_name).read().decode("utf-8")

        relative = {name.split("/", 1)[1] for name in members if "/" in name}
        for expected in (
            "pyproject.toml",
            "README.md",
            "LICENSE",
            "src/ralph/__init__.py",
            "src/ralph/cli.py",
            "PKG-INFO",
        ):
            self.assertIn(expected, relative, f"{expected} missing from sdist")
        metadata = email.message_from_string(pkg_info)
        self.assertEqual(metadata["Name"], "simple-ralph-loop")
        self.assertEqual(metadata["Version"], "0.1.0")
        self.assertEqual(metadata["License-Expression"], "MIT")
        self.assertIsNone(metadata.get_all("Requires-Dist"))


class WorktreeLockMetadataTest(unittest.TestCase):
    """Deterministic ownership-verification coverage for stale lock recovery."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.git_dir = self.base / "gitdir"
        self.git_dir.mkdir()
        self.meta = self.git_dir / "ralph" / "lock.json"

    def _write_meta(self, value: object) -> None:
        self.meta.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, str):
            self.meta.write_text(value, encoding="utf-8")
        else:
            self.meta.write_text(json.dumps(value), encoding="utf-8")

    def test_absent_metadata_acquires_and_records_owner(self) -> None:
        lock = cli.WorktreeLock(self.git_dir, self.meta)
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertTrue(lock.acquired)
        self.assertEqual(json.loads(self.meta.read_text())["pid"], os.getpid())

    def test_malformed_metadata_is_treated_as_stale_and_recovered(self) -> None:
        self._write_meta("{ not valid json")
        lock = cli.WorktreeLock(self.git_dir, self.meta)
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertTrue(lock.acquired)
        self.assertEqual(json.loads(self.meta.read_text())["pid"], os.getpid())

    def test_reused_pid_with_mismatched_identity_is_recovered(self) -> None:
        # The recorded PID is live (our own) but its identity does not match, as
        # happens when the OS reuses a dead owner's PID for an unrelated process.
        self._write_meta({"pid": os.getpid(), "identity": "not-the-real-identity"})
        lock = cli.WorktreeLock(self.git_dir, self.meta)
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertTrue(lock.acquired)

    def test_inconsistent_pid_type_is_recovered(self) -> None:
        self._write_meta({"pid": "not-an-int", "identity": "whatever"})
        lock = cli.WorktreeLock(self.git_dir, self.meta)
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertTrue(lock.acquired)

    def test_live_matching_owner_refuses_recovery(self) -> None:
        self._write_meta({"pid": os.getpid(), "identity": cli.process_identity(os.getpid())})
        lock = cli.WorktreeLock(self.git_dir, self.meta)
        with self.assertRaises(cli.RalphError) as caught:
            lock.acquire()
        self.assertIn("live matching owner", str(caught.exception))
        self.assertFalse(lock.acquired)

    def test_symlinked_metadata_file_is_refused(self) -> None:
        self.meta.parent.mkdir(parents=True)
        target = self.base / "outside.json"
        target.write_text("{}", encoding="utf-8")
        os.symlink(target, self.meta)
        lock = cli.WorktreeLock(self.git_dir, self.meta)
        with self.assertRaises(cli.RalphError) as caught:
            lock.acquire()
        self.assertIn("not a regular file", str(caught.exception))
        self.assertFalse(lock.acquired)

    def test_symlinked_state_root_is_refused(self) -> None:
        outside = self.base / "outside-root"
        outside.mkdir()
        os.symlink(outside, self.git_dir / "ralph")
        lock = cli.WorktreeLock(self.git_dir, self.meta)
        with self.assertRaises(cli.RalphError) as caught:
            lock.acquire()
        self.assertIn("symlink", str(caught.exception))
        self.assertFalse(lock.acquired)

    def test_release_after_refused_recovery_leaves_lock_free(self) -> None:
        self._write_meta({"pid": os.getpid(), "identity": cli.process_identity(os.getpid())})
        first = cli.WorktreeLock(self.git_dir, self.meta)
        with self.assertRaises(cli.RalphError):
            first.acquire()
        # The exclusive flock was released, so a clean record can be recovered.
        self.meta.unlink()
        second = cli.WorktreeLock(self.git_dir, self.meta)
        second.acquire()
        self.addCleanup(second.release)
        self.assertTrue(second.acquired)


if __name__ == "__main__":
    unittest.main()
