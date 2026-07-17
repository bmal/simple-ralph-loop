"""Secret redaction of streamed backend output while keeping JSON export
parseable, including secrets split across read chunks."""

from __future__ import annotations

import json

from harness import RalphCliTestCase


class RedactionTest(RalphCliTestCase):
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
