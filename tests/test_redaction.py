"""Secret redaction of streamed backend output while keeping JSON export
parseable, including secrets split across read chunks and secrets a Backend
spliced a control character into (register J2)."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time
import unicodedata
import unittest

from harness import RalphCliTestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ralph.redaction import (  # noqa: E402  (import after sys.path is extended)
    MIN_SECRET_LENGTH,
    REDACTION_PLACEHOLDER,
    Redactor,
)


class RedactionTest(RalphCliTestCase):
    def test_streamed_oauth_token_is_redacted_from_claude_streams(self) -> None:
        token = "oauth-subscription-token-1234567890"
        text = (
            f"Token is {token} inside output.\n"
            "Work complete.\n<promise>COMPLETE</promise>"
        )
        result = self.run_ralph(
            # Streamed backend text only reaches a stream under the opt-in feed, so
            # the leak this guards against is only reachable there (register G2/G11).
            "--verbose",
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
            "--verbose",
            env={
                "CLAUDE_CODE_OAUTH_TOKEN": token,
                "FAKE_EVENTS": text_event(first) + "\n" + text_event(full),
                "FAKE_EXPORT": self._export(full),
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(token, result.stdout)
        self.assertIn("[redacted]", result.stdout)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        self.assertNotIn(token, (run_dir / "stdout.ndjson").read_text())

    def test_secret_spliced_with_a_control_character_is_redacted_on_disk(self) -> None:
        # #57's reproduction of finding 18: a Backend echoes the credential with one
        # ESC spliced into it. Today the literal matcher misses it and the retained
        # stream carries the whole token, recoverable by deleting the one escape.
        token = "oauth-subscription-token-1234567890"
        spliced = token[:12] + "\x1b" + token[12:]
        text = (
            f"Echoed {spliced} back.\nWork complete.\n<promise>COMPLETE</promise>"
        )
        result = self.run_ralph(
            backend="claude",
            env={
                "CLAUDE_CODE_OAUTH_TOKEN": token,
                "FAKE_CLAUDE_EVENTS": self._claude_events(text),
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        stdout_ndjson = (run_dir / "stdout.ndjson").read_text()
        # The stream is NDJSON, so the spliced ESC is on disk as its JSON escape.
        # A reader that un-escapes the line recovers the credential unless the whole
        # spliced span was redacted.
        self.assertNotIn(token, stdout_ndjson)
        self.assertNotIn(token, _unescaped(stdout_ndjson))
        self.assertIn(REDACTION_PLACEHOLDER, stdout_ndjson)

    def test_secret_spliced_into_a_diagnostic_stream_is_redacted_on_disk(self) -> None:
        # The same evasion where the codepoint reaches disk raw rather than escaped:
        # the Backend's own stderr is retained verbatim, so an ESC and a zero-width
        # space each survive into the log as themselves.
        token = "oauth-subscription-token-1234567890"
        escaped = token[:12] + "\x1b" + token[12:]
        zero_width = token[:20] + "\u200b" + token[20:]
        raw_stderr = self.base / "spliced-stderr.txt"
        raw_stderr.write_text(
            f"boom {escaped} and {zero_width} done\n", encoding="utf-8"
        )
        result = self.run_ralph(
            backend="claude",
            env={
                "CLAUDE_CODE_OAUTH_TOKEN": token,
                "FAKE_CLAUDE_EVENTS": self._claude_events(
                    "Work complete.\n<promise>COMPLETE</promise>"
                ),
                "FAKE_CLAUDE_RAW_STDERR_FILE": str(raw_stderr),
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        stderr_log = (run_dir / "stderr.log").read_text()
        self.assertNotIn(token, _stripped(stderr_log))
        self.assertEqual(stderr_log.count(REDACTION_PLACEHOLDER), 2)


def _stripped(text: str) -> str:
    """What an operator recovers from a retained stream by deleting the control and
    format codepoints a Backend spliced into it."""
    return "".join(
        char for char in text if unicodedata.category(char) not in ("Cc", "Cf")
    )


def _unescaped(text: str) -> str:
    """What a reader recovers from a retained NDJSON stream by resolving the JSON
    escapes in it and then deleting the codepoints they named."""
    return _stripped(text.encode("utf-8", "surrogatepass").decode("unicode_escape"))


class RedactorMatchTest(unittest.TestCase):
    """The matcher behind the choke point, exercised directly: what counts as a
    match widens, what counts as a secret does not, and an unmatched stream is
    returned unchanged."""

    def setUp(self) -> None:
        self.token = "oauth-subscription-token-1234567890"
        self.redactor = Redactor([self.token])

    def test_plain_secret_is_still_redacted(self) -> None:
        self.assertEqual(
            self.redactor.scrub(f"a {self.token} b"), f"a {REDACTION_PLACEHOLDER} b"
        )

    def test_every_single_splice_position_is_redacted(self) -> None:
        for splice in ("\x1b", "\u200b", "\x07", "\ufeff", "\r"):
            for cut in (1, len(self.token) // 2, len(self.token) - 1):
                payload = self.token[:cut] + splice + self.token[cut:]
                with self.subTest(splice=repr(splice), cut=cut):
                    self.assertEqual(
                        self.redactor.scrub(f"< {payload} >"),
                        f"< {REDACTION_PLACEHOLDER} >",
                    )

    def test_the_whole_spliced_span_is_replaced(self) -> None:
        # The placeholder cannot be un-spliced: no fragment of the secret and none of
        # the characters spliced between them survive the substitution.
        payload = "\x1b".join(self.token)
        scrubbed = self.redactor.scrub(f"< {payload} >")
        self.assertEqual(scrubbed, f"< {REDACTION_PLACEHOLDER} >")
        self.assertNotIn("\x1b", scrubbed)

    def test_a_splice_named_by_a_json_escape_is_redacted(self) -> None:
        # What a retained NDJSON stream actually holds: the spliced codepoint is six
        # ASCII characters naming it, not the codepoint.
        for escape in ("\\u001b", "\\u200B", "\\n", "\\t", "\\udb40\\udc20"):
            payload = self.token[:12] + escape + self.token[12:]
            with self.subTest(escape=escape):
                self.assertEqual(
                    self.redactor.scrub(f'"text": "{payload}"'),
                    f'"text": "{REDACTION_PLACEHOLDER}"',
                )

    def test_a_secret_escaped_and_spliced_inside_a_json_string_is_redacted(self) -> None:
        # A secret that itself needs escaping reaches disk in its escaped form; the
        # widened match has to hold for that variant too.
        secret = 'quote"and\\backslash-1234567890'
        redactor = Redactor([secret])
        on_disk = json.dumps(secret)[1:-1]
        payload = on_disk[:10] + "\\u0007" + on_disk[10:]
        self.assertEqual(
            redactor.scrub(f'"text": "{payload}"'),
            f'"text": "{REDACTION_PLACEHOLDER}"',
        )

    def test_a_json_escape_that_names_no_control_codepoint_is_not_a_separator(
        self,
    ) -> None:
        # Widening the match must not turn every backslash escape into glue: a
        # letter named by a JSON escape is content, so the fragments either side of
        # it are not the secret.
        payload = self.token[:12] + "\\u0041" + self.token[12:]
        self.assertEqual(self.redactor.scrub(payload), payload)

    def test_short_values_are_still_never_redacted(self) -> None:
        short = "a" * (MIN_SECRET_LENGTH - 1)
        redactor = Redactor([short])
        self.assertFalse(redactor)
        text = f"flag {short} and {short[:2]}\x1b{short[2:]}"
        self.assertEqual(redactor.scrub(text), text)

    def test_a_stream_without_the_secret_is_returned_unchanged(self) -> None:
        # #36's artifact rule: only matched spans change on disk.
        stream = "".join(
            json.dumps({"type": "text", "text": f"line {index} \x1b[31m\u200b ok"})
            + "\n"
            for index in range(500)
        )
        self.assertEqual(self.redactor.scrub(stream), stream)

    def test_near_miss_prefixes_do_not_backtrack_catastrophically(self) -> None:
        # The shape that makes a badly built pattern take exponential time: 500 copies
        # of the whole secret bar its last character, each one saturated with the
        # separators -- both spellings -- so every position drives the widened match
        # as deep as it can go before failing. A pattern that backtracks never
        # returns; the bound is generous enough that only a real blow-up trips it.
        near_miss = "".join(
            "\u200b\\u001b".join(self.token[:-1]) + "!" for _ in range(500)
        )
        start = time.perf_counter()
        scrubbed = self.redactor.scrub(near_miss)
        elapsed = time.perf_counter() - start
        self.assertEqual(scrubbed, near_miss)
        self.assertLess(elapsed, 2.0, "the control-insensitive pattern backtracked")
