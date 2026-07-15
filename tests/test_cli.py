from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
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
              "--pure export "*) printf '%s\\n' "${FAKE_EXPORT}" ;;
              *" run "*)
                cat > "$FAKE_CALLS/stdin"
                env | sort > "$FAKE_CALLS/env"
                printf '%s\\n' "${FAKE_EVENTS}"
                printf '%s\\n' "backend diagnostic" >&2
                exit "${FAKE_EXIT:-0}"
                ;;
              *) exit 2 ;;
            esac
            """,
        )

    def _events(self, text: str, model: str = "gpt-5.6-sol") -> str:
        del model
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

    def _export(self, text: str, model: str = "gpt-5.6-sol") -> str:
        return json.dumps(
            {
                "info": {"id": "ses_1"},
                "messages": [
                    {
                        "info": {
                            "id": "msg_1",
                            "sessionID": "ses_1",
                            "role": "assistant",
                            "providerID": "openai",
                            "modelID": model,
                        },
                        "parts": [{"id": "part_1", "type": "text", "text": text}],
                    }
                ],
            }
        )

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

    def run_ralph(self, *extra: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
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
        return subprocess.run(
            [
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
                str(self.repo),
                *extra,
            ],
            cwd=self.base,
            env=child_env,
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
        self.assertIn("exact standalone line", composed)
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
