"""Shared black-box test harness: fake backends on PATH, the subprocess
command builder, the sanitized-environment allowlist, and the run/clean/
resume helpers. Every behavior-area test case subclasses RalphCliTestCase
so this harness lives in exactly one home."""

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
sys.path.insert(0, str(ROOT / "src"))


class RalphCliTestCase(unittest.TestCase):
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
        # Stand-in for `/usr/bin/sandbox-exec`: records the wrap arguments
        # (including the -f <profile> path) so tests can assert the launch chain,
        # then drops `-f <profile>` and execs the confined command. Production
        # uses the real Seatbelt launcher; this only proves argv construction.
        self._script(
            "sandbox-exec",
            """
            printf '%s\\n' "$*" >> "$FAKE_CALLS/sandbox-exec"
            test "$1" = "-f"
            shift 2
            # The one-shot host-isolation self-test (register D8) probes the
            # profile through sandbox-exec too. This fake cannot enforce Seatbelt,
            # so it *simulates* the kernel's verdict for a recognizable probe: a
            # correct profile refuses the denied read (~/Library/Keychains) and
            # the denied write (the self-test write probe), exiting non-zero,
            # unless a test opts that probe open via FAKE_SELFTEST_ALLOW. Every
            # other invocation is a real backend launch and is exec'd unchanged.
            probe=""
            case "$*" in
              *.ralph-sandbox-selftest-write-probe*) probe=write ;;
              *Library/Keychains*) probe=read ;;
            esac
            if test -n "$probe"; then
              case " ${FAKE_SELFTEST_ALLOW:-} " in
                *" $probe "*) exit 0 ;;
              esac
              exit 1
            fi
            exec "$@"
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
                if test -n "${FAKE_CLAUDE_ERROR_RESULT_ON_INT:-}"; then
                  trap 'printf "%s\n" "$FAKE_CLAUDE_ERROR_RESULT_ON_INT"; exit 0' INT
                  while :; do sleep 1 || true; done
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

    def _config(self, agents: dict | None = None) -> str:
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
                # OpenCode surfaces every loaded agent (project `.opencode/agent`
                # and global definitions, even under --pure) in this map, so an
                # empty map is the effective-config proof of agent isolation.
                "agent": agents or {},
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
                "apiKeySource": "none",
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
                # Absolute path to the fake sandbox-exec: production uses
                # /usr/bin/sandbox-exec, and this test-only seam substitutes it.
                "RALPH_SANDBOX_EXEC": str(self.bin / "sandbox-exec"),
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

    def _run_backend_question(self, backend: str, text: str) -> subprocess.CompletedProcess[str]:
        if backend == "claude":
            return self.run_ralph(
                backend="claude", env={"FAKE_CLAUDE_EVENTS": self._claude_events(text)}
            )
        return self.run_ralph(
            env={"FAKE_EVENTS": self._events(text), "FAKE_EXPORT": self._export(text)}
        )

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

    def _ralph_state(self) -> Path:
        return self.repo / ".git" / "ralph"
