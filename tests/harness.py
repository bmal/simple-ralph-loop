"""Shared black-box test harness: fake backends on PATH, the subprocess
command builder, the sanitized-environment allowlist, the run/clean/
resume helpers, and the narrow pseudo-terminal capability. Every behavior-area
test case subclasses RalphCliTestCase so this harness lives in exactly one home.

``PtyCapture`` and ``run_ralph_pty`` exist because the suite otherwise drives
Ralph exclusively through pipes, which would leave the terminal path — colour and
width — as the one path nothing exercises, and the degraded piped path free to rot
unnoticed (register G20). The capability is deliberately narrow: a handful of
tests, asserting what an operator on a terminal is shown, never how it is
painted."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import pty
import struct
import subprocess
import sys
import tempfile
import termios
import textwrap
import threading
import time
from typing import TextIO
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class PtyCapture:
    """A pseudo-terminal of a known width, and what was written to it.

    Used as a context manager. The master end is drained by a thread for the whole
    lifetime, so a writer that outruns the pty's own buffer cannot block; ``text``
    is readable once the block exits, with the CRLF a pty puts on every line
    normalized away so assertions stay about *what* was said."""

    def __init__(self, columns: int = 100) -> None:
        self._columns = columns
        self._chunks: list[bytes] = []

    def __enter__(self) -> PtyCapture:
        self._master, self.fd = pty.openpty()
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, self._columns, 0, 0))
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()
        return self

    def __exit__(self, *_: object) -> None:
        # The slave must close before the master can see end of file: it is the
        # last writer once the process or stream under test has finished with it.
        os.close(self.fd)
        self._reader.join(timeout=10)
        os.close(self._master)

    def text_stream(self) -> TextIO:
        """A text stream onto the terminal that owns its own descriptor, so closing
        it cannot pull the slave out from under the capture."""
        return os.fdopen(os.dup(self.fd), "w", encoding="utf-8")

    @property
    def text(self) -> str:
        return b"".join(self._chunks).decode("utf-8", "replace").replace("\r\n", "\n")

    def _drain(self) -> None:
        while True:
            try:
                data = os.read(self._master, 65536)
            except OSError:
                # The last writer closed the slave: macOS reports EIO here rather
                # than a clean end of file.
                return
            if not data:
                return
            self._chunks.append(data)


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
              "issue list")
                # Resolving the interactive-only children (#34). A test opts a
                # non-zero exit in with FAKE_GH_ISSUE_LIST_FAIL and malformed
                # output in with FAKE_GH_ISSUE_LIST_MALFORMED; otherwise the
                # listing is FAKE_GH_ISSUE_LIST, defaulting to the well-formed
                # empty array gh returns when no issue carries the label.
                test "${FAKE_GH_ISSUE_LIST_FAIL:-0}" = "0" || exit 1
                if test -n "${FAKE_GH_ISSUE_LIST_MALFORMED:-}"; then
                  printf '%s\\n' "${FAKE_GH_ISSUE_LIST_MALFORMED}"
                else
                  printf '%s\\n' "${FAKE_GH_ISSUE_LIST:-[]}"
                fi
                ;;
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
              # Publish our own pid so a test can model a loop-wide assertion
              # lost mid-run by killing it on command (FAKE_KILL_CAFFEINATE in
              # the backend). Dying on command rather than on a wall-clock timer
              # keeps that scenario deterministic under CI scheduling latency.
              printf '%s\\n' "$$" > "$FAKE_CALLS/caffeinate-pid"
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
                emit_export() {
                  if test -n "${FAKE_SEQUENCE_DIR:-}"; then
                    cat "$FAKE_SEQUENCE_DIR/export-$1"
                  else
                    printf '%s\\n' "${FAKE_EXPORT}"
                  fi
                }
                if test -n "${FAKE_EXPORT_PIPE_TRUNCATION:-}" && test -p /dev/fd/1; then
                  # Model the real CLI: the export is one large write followed by
                  # an immediate exit, so a pipe keeps only the prefix that had
                  # landed -- and the command still exits 0. A regular file keeps
                  # the whole payload, which is what Ralph must capture into.
                  emit_export "${3}" | head -c "$FAKE_EXPORT_PIPE_TRUNCATION"
                  exit 0
                fi
                emit_export "${3}"
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
                if test -n "${FAKE_KILL_CAFFEINATE:-}"; then
                  # Model the loop-wide power assertion dying mid-iteration by
                  # killing it while this iteration is still running. We only
                  # send the signal -- never wait for the process to vanish: its
                  # parent (ralph) reaps it lazily via poll() at the next
                  # boundary, so it lingers as a zombie until then and a
                  # `kill -0` wait here would deadlock the iteration. Ralph's
                  # ensure_alive() therefore observes it dead only after this
                  # iteration's evidence is already retained.
                  caffeinate_pid=$(cat "$FAKE_CALLS/caffeinate-pid" 2>/dev/null || true)
                  if test -n "$caffeinate_pid"; then
                    kill -KILL "$caffeinate_pid" 2>/dev/null || true
                  fi
                fi
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
                if test -n "${FAKE_CLAUDE_SEQUENCE_DIR:-}"; then
                  count_file="$FAKE_CALLS/claude-run-count"
                  count=0
                  test ! -f "$count_file" || count=$(cat "$count_file")
                  count=$((count + 1))
                  printf '%s\n' "$count" > "$count_file"
                  cat > "$FAKE_CALLS/claude-stdin-$count"
                  cat "$FAKE_CLAUDE_SEQUENCE_DIR/events-$count"
                else
                  cat > "$FAKE_CALLS/claude-stdin"
                  printf '%s\n' "${FAKE_CLAUDE_EVENTS}"
                fi
                env | sort > "$FAKE_CALLS/claude-env"
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

    def _claude_sequence(self, streams: list[str]) -> Path:
        # One stdout stream per `claude -p` call, so a test can give consecutive
        # iterations different behaviour across a multi-iteration run.
        sequence = self.base / "claude-sequence"
        sequence.mkdir()
        for index, events in enumerate(streams, 1):
            (sequence / f"events-{index}").write_text(events + "\n", encoding="utf-8")
        return sequence

    # Atomic Claude stream-event builders. Multi-turn tests compose these directly
    # so a stream's exact shape -- interleaved inits, a background registration,
    # tagged subagent messages, results flushed at EOF -- is spelled out in the
    # test that depends on it.
    def _claude_init_event(
        self, session_id: str, model: str = "claude-opus-4-8", **overrides: object
    ) -> dict:
        event = {
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
        }
        event.update(overrides)
        return event

    def _claude_assistant_event(
        self,
        text: str,
        session_id: str,
        model: str = "claude-opus-4-8",
        parent_tool_use_id: str | None = None,
    ) -> dict:
        # `parent_tool_use_id` is None for the Backend's own messages and a
        # tool-use id for a background subagent's.
        return {
            "type": "assistant",
            "session_id": session_id,
            "parent_tool_use_id": parent_tool_use_id,
            "message": {
                "id": "msg_claude",
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": text}],
            },
        }

    def _claude_background_event(self, session_id: str) -> dict:
        # Registers a background subagent; this is what licenses a later init.
        return {
            "type": "system",
            "subtype": "background_tasks_changed",
            "session_id": session_id,
            "tasks": [{"task_id": "t1", "task_type": "local_agent", "description": "survey"}],
        }

    def _claude_result_event(
        self,
        text: str,
        session_id: str,
        model: str = "claude-opus-4-8",
        model_usage: dict | None = None,
    ) -> dict:
        return {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": session_id,
            "result": text,
            "modelUsage": model_usage or {model: {"inputTokens": 1, "outputTokens": 1}},
        }

    def _claude_clean_drain_teardown(self, session_id: str) -> list[dict]:
        # The teardown tail Claude Code 2.1.228 emits after a session that
        # drained its background task cleanly mid-turn: a single telemetry
        # summary after the result block. Synthesised to the observed shape
        # (H12); no captured or probe stream is committed.
        return [
            {
                "type": "system",
                "subtype": "task_summary",
                "session_id": session_id,
                "summary": {"completed": 1, "killed": 0},
            },
        ]

    def _claude_park_teardown(
        self, session_id: str, task_id: str = "t1", description: str = "verify CI"
    ) -> list[dict]:
        # The teardown tail after a park: the Backend ended its turn with a task
        # still in flight, so the CLI drained the task list, killed the task,
        # notified that it stopped, and summarised -- all after the result block.
        # Synthesised to the observed shape (H12); no captured stream is committed.
        return [
            {
                "type": "system",
                "subtype": "background_tasks_changed",
                "session_id": session_id,
                "tasks": [],
            },
            {
                "type": "system",
                "subtype": "task_updated",
                "session_id": session_id,
                "task_id": task_id,
                "patch": {"status": "killed", "end_time": 1, "description": description},
            },
            {
                "type": "system",
                "subtype": "task_notification",
                "session_id": session_id,
                "task_id": task_id,
                "status": "stopped",
            },
            {
                "type": "system",
                "subtype": "task_summary",
                "session_id": session_id,
                "summary": {"completed": 0, "killed": 1},
            },
        ]

    @staticmethod
    def _claude_stream(events: list[dict]) -> str:
        return "\n".join(json.dumps(event) for event in events)

    def _claude_multiturn_events(
        self,
        turns: list[dict],
        session_id: str = "claude-session-1",
        model: str = "claude-opus-4-8",
        teardown: list[dict] | None = None,
    ) -> str:
        # Compose an N-turn stream in the observed shape: each init opens a turn
        # interleaved with that turn's work, a `background_tasks_changed`
        # registers before the next init licenses it, and every turn's result is
        # flushed at EOF in turn order. Each entry of `turns` is a dict with
        # "text" (the turn's final Backend message and its result) and optional
        # "subagents" (subagent message texts, tagged with parent_tool_use_id).
        # An optional `teardown` tail (see the clean-drain and park builders) is
        # appended after the result block, modelling the events the real CLI
        # emits at teardown when a session touched a background task.
        events: list[dict] = []
        for index, turn in enumerate(turns):
            events.append(self._claude_init_event(session_id, model))
            for order, subagent in enumerate(turn.get("subagents", [])):
                events.append(
                    self._claude_assistant_event(
                        subagent, session_id, model, parent_tool_use_id=f"toolu_{index}_{order}"
                    )
                )
            events.append(self._claude_assistant_event(turn["text"], session_id, model))
            if index < len(turns) - 1:
                events.append(self._claude_background_event(session_id))
        for turn in turns:
            events.append(self._claude_result_event(turn["text"], session_id, model))
        if teardown:
            events.extend(teardown)
        return self._claude_stream(events)

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

    def run_ralph_pty(
        self,
        *extra: str,
        env: dict[str, str] | None = None,
        backend: str = "opencode",
        columns: int = 100,
        timeout: float = 60,
    ) -> subprocess.CompletedProcess[str]:
        """Run Ralph with stderr attached to a pseudo-terminal of a known width,
        returning what an operator sitting at that terminal would have seen.

        The child is handed the slave end directly rather than being adopted into
        its own session: ``isatty`` and the window-size ioctl -- the two facts the
        Run console reads -- answer identically, and the run stays an ordinary
        subprocess we can wait on."""
        with PtyCapture(columns) as terminal:
            process = subprocess.run(
                self._command("run", *extra, backend=backend),
                cwd=self.base,
                env=self._environment(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=terminal.fd,
                timeout=timeout,
            )
        return subprocess.CompletedProcess(
            process.args,
            process.returncode,
            process.stdout.decode("utf-8", "replace"),
            terminal.text,
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
