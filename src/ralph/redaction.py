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

Depends on / must not know: ``environment`` (for ``LLM_ENV_VARS``). It must not
know which stream is being scrubbed.

See also: ``environment`` (owns the LLM ban-list), ``gitcontext`` (write_json
scrubs through ``redact``), the Backend adapters (scrub live output and stderr).
"""

from __future__ import annotations

import json
import os

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

    def scrub(self, text: str) -> str:
        if not self._variants or not text:
            return text
        for variant in self._variants:
            if variant in text:
                text = text.replace(variant, REDACTION_PLACEHOLDER)
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
