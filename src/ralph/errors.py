"""Ralph's exception hierarchy: fatal errors and consuming handoffs.

Invariants:
- ``RalphError`` is the single family every command catches at the top level to
  print ``ralph: <message>`` and exit; anything else escapes as a crash.
- A ``HandoffError`` means an Iteration reached a resumable stopping point after a
  backend session existed: it carries the session id, the operator-facing detail,
  and the outcome so the Loop can record it and print a resume command. A
  ``StartedIterationError`` is the same idea before any session metadata arrived —
  the Iteration slot is consumed but there is nothing to resume.
- A ``RetryableIterationError`` is a ``HandoffError`` the Loop is *allowed* to
  absorb: the attempt produced no outcome, but the failure is one a fresh session
  can be steered past, so the Loop may spend the same budget slot again on a
  replacement attempt carrying the backend-supplied ``correction``. It remains a
  ``HandoffError`` so the path that cannot retry — the replacement allowance spent
  — hands off exactly as an ordinary contract failure does, charging the started
  Iteration.
- ``raise_backend_contract_failure`` encodes the shared rule both adapters obey: a
  contract failure *after* a session exists is a resumable, consuming handoff;
  before any session it is an ordinary pre-session ``RalphError``.

Depends on / must not know: nothing. This module is a leaf; it must not import
any other Ralph module so every other module can raise these without a cycle.

See also: ``process`` (raise_if_controlled_stop builds these from a stop),
``loop`` (catches them to record outcomes and print handoffs).
"""

from __future__ import annotations


class RalphError(Exception):
    pass


class HandoffError(RalphError):
    def __init__(
        self,
        reason: str,
        session_id: str,
        detail: str | None = None,
        outcome: str = "needs_input",
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.session_id = session_id
        self.detail = detail
        self.outcome = outcome


class RetryableIterationError(HandoffError):
    def __init__(
        self,
        reason: str,
        session_id: str,
        correction: str,
        outcome: str = "backend_contract_failure",
    ) -> None:
        super().__init__(reason, session_id, outcome=outcome)
        # Backend-authored text the Loop appends to the replacement Iteration's
        # prompt. The Loop never composes it: only the adapter knows what the
        # backend did wrong and what wording steers a fresh session past it.
        self.correction = correction


class StartedIterationError(RalphError):
    def __init__(self, reason: str, outcome: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.outcome = outcome


def raise_backend_contract_failure(session_id: str | None, message: str) -> None:
    # A contract failure after a session exists is a resumable, consuming
    # handoff; before any session it is an ordinary pre-session failure.
    if session_id:
        raise HandoffError(message, session_id, outcome="backend_contract_failure")
    raise RalphError(message)
