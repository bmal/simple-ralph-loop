"""Secret collection, the ``Redactor``, and the process-wide active-redactor
functions that scrub subscription credentials from every retained/printed stream.

Invariants:
- Import the *functions* ``redact`` / ``set_active_redactor``, never the
  ``_ACTIVE_REDACTOR`` global. ``set_active_redactor`` rebinds the module global to
  a fresh ``Redactor``; a caller that captured the old object (``from .redaction
  import _ACTIVE_REDACTOR``) would keep scrubbing against a stale, empty set and
  silently leak. Going through ``redact()`` always reads the current binding.
- ``SECRET_ENV_VARS`` are the credentials that legitimately reach the child or the
  operator's shell and could be echoed back; their values are redacted defensively
  even though the unsafe-environment refusal already bars API keys before a run.
- Values shorter than ``MIN_SECRET_LENGTH`` are never redacted: they are
  indistinguishable from ordinary tokens (flags, booleans) and scrubbing them
  would corrupt unrelated output. A secret's JSON-escaped form is redacted too, so
  a value embedded in a JSON string cannot slip through raw-line control parsing.
- The matcher is control-insensitive, not a literal substring search (register J2).
  A secret matches across control and format codepoints spliced between its
  characters, in either spelling a retained stream can hold them: the codepoint
  itself, as a Backend's own diagnostic output carries it, and the JSON escape
  naming it, as the same codepoint becomes inside an NDJSON string. One ``ESC`` or
  one zero-width space would otherwise defeat redaction entirely. What widens is
  what counts as a *match*, never what counts as a secret: ``MIN_SECRET_LENGTH``
  and the variant set are unchanged.
- The whole matched span is replaced, the spliced characters included, so the
  placeholder cannot be un-spliced back into the credential. Nothing outside a
  matched span is touched, which is what keeps a retained artifact that holds no
  secret byte-identical.
- The pattern is built from ``unicodedata`` at the first ``Redactor`` that has a
  secret to match, never at import: enumerating the ``Cc``/``Cf`` categories walks
  the whole Unicode range, and a run whose environment holds no credential must not
  pay for it.

Depends on / must not know: ``environment`` (for ``LLM_ENV_VARS``). It must not
know which stream is being scrubbed.

See also: ``environment`` (owns the LLM ban-list), ``gitcontext`` (write_json
scrubs through ``redact``), the Backend adapters (scrub live output and stderr),
``console`` (holds the other half of register J2: it neutralises a Backend-authored
string before handing it here, so the console path never needs the widened match).
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata

from .environment import LLM_ENV_VARS


# Subscription credentials that legitimately reach the child environment or an
# operator's shell and could be echoed back through backend output. API-key and
# custom-endpoint variables are refused before a session starts, but their
# values are still redacted defensively if they ever appear in retained streams.
SECRET_ENV_VARS = {"CLAUDE_CODE_OAUTH_TOKEN"} | {
    name
    for name in LLM_ENV_VARS
    if any(marker in name for marker in ("API_KEY", "AUTH_TOKEN", "TOKEN", "HEADERS", "CREDENTIAL"))
}
REDACTION_PLACEHOLDER = "[redacted]"
# Values shorter than this are indistinguishable from ordinary tokens (flags,
# booleans) and redacting them would corrupt unrelated output. Real credentials
# are far longer, so a conservative floor keeps redaction precise.
MIN_SECRET_LENGTH = 8
# The categories a Backend can splice between two characters of a secret without
# changing what a reader recovers from the line. The same pair the console's
# neutraliser removes, so the two halves of register J2 draw one boundary.
_CONTROL_CATEGORIES = ("Cc", "Cf")
_SPLICE_SEPARATOR: str | None = None


def _codepoint_escape(codepoint: int) -> str:
    """A codepoint as a regex escape, so a character class never carries a literal
    control character or a class metacharacter."""
    return f"\\u{codepoint:04x}" if codepoint <= 0xFFFF else f"\\U{codepoint:08x}"


def _json_escape(codepoint: int) -> str:
    """The text a JSON encoder writes for a codepoint inside a string. Always the
    ``\\uXXXX`` form for the BMP -- an encoder may write either that or the short
    ``\\n``-style spelling for the five that have one, and both are matched -- and the
    surrogate pair for anything above it."""
    if codepoint <= 0xFFFF:
        return f"\\u{codepoint:04x}"
    return json.dumps(chr(codepoint))[1:-1]


def _splice_separator() -> str:
    """The regex fragment matching a run of spliced control and format codepoints,
    built once per process.

    Both spellings a retained stream can hold are matched: the codepoint itself, and
    the JSON escape naming it. Only escapes naming a ``Cc``/``Cf`` codepoint count --
    ``\\u0041`` is the letter A, content rather than glue, and joining two fragments
    across one would redact text that is not the secret.

    The escape forms are matched in any stream, not only a JSON one: nothing here knows
    which stream it is scrubbing, and that is the invariant this module keeps. The cost
    is a shade of over-generality -- a literal ``\\n`` in prose counts as glue too --
    paid only *inside* text that already spells the secret either side of it.

    The alternatives are mutually exclusive -- a control codepoint is never a
    backslash, and the two escape forms differ on their second character -- so the
    surrounding ``*`` has one way to match any input and cannot backtrack."""
    global _SPLICE_SEPARATOR
    if _SPLICE_SEPARATOR is not None:
        return _SPLICE_SEPARATOR
    codepoints = [
        codepoint
        for codepoint in range(sys.maxunicode + 1)
        if unicodedata.category(chr(codepoint)) in _CONTROL_CATEGORIES
    ]
    spans: list[str] = []
    start = previous = codepoints[0]
    for codepoint in codepoints[1:] + [-1]:
        if codepoint == previous + 1:
            previous = codepoint
            continue
        spans.append(
            _codepoint_escape(start)
            if start == previous
            else f"{_codepoint_escape(start)}-{_codepoint_escape(previous)}"
        )
        start = previous = codepoint
    # The escapes are grouped on their shared prefix so the fragment stays small: it
    # is repeated between every pair of characters of every secret.
    tails: dict[str, set[str]] = {}
    for codepoint in codepoints:
        escape = _json_escape(codepoint)
        tails.setdefault(escape[:-1], set()).add(escape[-1])
    escapes = []
    for prefix, finals in sorted(tails.items()):
        digits = "".join(sorted(finals))
        escapes.append(re.escape(prefix) + (f"[{digits}]" if len(digits) > 1 else re.escape(digits)))
    literal = "".join(spans)
    # The five short JSON escapes all name a control character, so one alternative
    # covers them; the hexadecimal digits of the long form are matched in either case.
    _SPLICE_SEPARATOR = f"(?:[{literal}]|\\\\[bfnrt]|(?i:{'|'.join(escapes)}))*"
    return _SPLICE_SEPARATOR


def _placeholder(_match: re.Match[str]) -> str:
    """A function rather than a replacement string, so the placeholder is written out
    as itself: ``re.sub`` reads a replacement string for group references, and this one
    must survive byte-identical (register J5) whatever it is ever changed to."""
    return REDACTION_PLACEHOLDER


class Redactor:
    def __init__(self, secrets: list[str]) -> None:
        variants: set[str] = set()
        for value in secrets:
            if not value or len(value) < MIN_SECRET_LENGTH:
                continue
            variants.add(value)
            # A secret embedded in a JSON string is escaped; redact that form too
            # so control-flow parsing (which reads the raw line) stays intact.
            escaped = json.dumps(value)[1:-1]
            if escaped != value:
                variants.add(escaped)
        self._variants = sorted(variants, key=len, reverse=True)
        separator = _splice_separator() if self._variants else ""
        self._patterns = [
            re.compile(separator.join(re.escape(char) for char in variant))
            for variant in self._variants
        ]

    def scrub(self, text: str) -> str:
        if not self._patterns or not text:
            return text
        for pattern in self._patterns:
            text = pattern.sub(_placeholder, text)
        return text

    def __bool__(self) -> bool:
        return bool(self._variants)


_ACTIVE_REDACTOR = Redactor([])


def redact(text: str) -> str:
    return _ACTIVE_REDACTOR.scrub(text)


def collect_secrets() -> list[str]:
    return [os.environ[name] for name in SECRET_ENV_VARS if os.environ.get(name)]


def set_active_redactor(secrets: list[str]) -> None:
    global _ACTIVE_REDACTOR
    _ACTIVE_REDACTOR = Redactor(secrets)
