"""The Backend package: per-backend default models, the Backend Protocol contract,
and the registry that resolves a backend name to its adapter module.

Invariants:
- ``DEFAULT_MODELS`` names the model each Backend runs when ``--model`` is omitted;
  the announced routing reflects it so a run always states what it will spend on.
- ``resolve`` is the single place a backend name becomes a Backend: it maps the name
  to the adapter module exactly once per invocation (register E1/E8). The loop and
  cli carry no ``backend == ...`` dispatch -- they drive the resolved Backend only
  through the five Protocol names and so cannot tell the two backends apart
  (register E2, user story 6). The only backend-name branching left anywhere is
  ``launch``'s host-isolation policy (which credential store to deny, which backend
  is wrapped today); that selects Launch-chain policy, never an adapter's behavior.
- ``Backend`` pins those five names — ``preflight``, ``validate_model``,
  ``execute_iteration``, ``resume_argv``, ``environment`` — for type-checkers only;
  the adapters are plain modules, matched structurally with no runtime class or ABC
  machinery (register E1).
- ``preflight`` returns a ``console.Deviation`` (or ``None``): when it admits an
  agent vector under ``--unsafe-allow-agents``, it hands the fact back so the caller
  states the relaxed guarantee loudly through the Run console (register G7/G14). The
  adapter constructs no operator-facing warning; the console owns the wording.
- ``execute_iteration`` returns ``(outcome, session_id, concluding_message)``: the
  Iteration's outcome, the session to resume, and the Backend's final message — the
  same text the completion/needs-input markers were read from — which the Loop hands
  to the Run console for the Iteration's outcome block. The message is a raw fact, not
  formatted text; the console truncates it for display only (register G14).
  The outcome names what happened to *the Iteration* — ``complete`` when the Backend
  declared completion, ``incomplete`` when the Iteration ended normally without doing
  so — never the run-level word for a spent budget, which only the Loop can decide
  and only once the whole budget is gone (register J3). A stop that ends the
  Iteration raises instead, carrying its own outcome.
- ``execute_iteration`` also receives an ``ObservationSink``: the narrow one-method
  seam (``observe``) declared here alongside the Backend Protocol and injected by the
  Loop, over the closed set of frozen ``console.Observation`` value types (register
  G15). The adapter emits facts through it -- tool use, orchestrator context, the
  live subagent roster, the Stage it declared through the Loop protocol, the mid-run
  warnings it used to print itself, and its own running commentary -- and the Run
  console words and renders them, including whether the commentary is shown at all
  (suppressed by default, restored under the opt-in feed, register G2/G11). No
  adapter writes to a terminal (register G13). This is a value type, not a wider
  interface: a new Observation is a new type, never a new method, so the adapters
  keep seeing one.
  The default ``None`` sink is a no-op, so an adapter that emits nothing (or a caller
  that injects none) still runs.

Depends on / must not know: the two adapter modules (imported so the registry can
name them) and ``console`` (for the ``Deviation`` value type ``preflight`` returns
and the ``Observation`` union the sink carries). It must not grow backend-specific
logic — that belongs in the adapters.

See also: ``backends.opencode`` / ``backends.claude`` (the adapters), ``loop`` and
``cli`` (resolve once, then drive the Backend through the five names), ``launch``
(the wrapped argv the adapters obtain).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..console import Deviation, Observation
from . import claude, opencode


DEFAULT_MODELS = {
    "claude": "claude-opus-5",
    "opencode": "openai/gpt-5.6-sol",
}


class ObservationSink(Protocol):
    """The narrow one-method seam the Backend adapters emit progress through
    (register G14/G15). Declared alongside the Backend Protocol because it is
    injected into ``execute_iteration``; the concrete sink is the Run console, which
    the composition root injects, but the adapters depend only on this one method."""

    def observe(self, observation: Observation) -> None: ...


class Backend(Protocol):
    """The five-name Backend interface (register E2). Everything else an adapter
    does — event accumulation, iteration consumption, session persistence,
    OpenCode's second-pass verification — is adapter-private and invisible here."""

    def preflight(
        self, worktree: Path, slug: str, model: str, env: dict[str, str], allow_agents: bool = ...
    ) -> Deviation | None: ...

    def validate_model(self, model: str) -> None: ...

    def execute_iteration(
        self,
        worktree: Path,
        run_dir: Path,
        prompt: str,
        model: str,
        env: dict[str, str],
        timeout: float,
        sandbox_profile: Path | None = ...,
        observe: ObservationSink | None = ...,
    ) -> tuple[str, str | None, str | None]: ...

    def resume_argv(self, worktree: Path, model: str, session: str) -> list[str]: ...

    def environment(self, model: str) -> dict[str, str]: ...


_BACKENDS: dict[str, Backend] = {"claude": claude, "opencode": opencode}


def resolve(backend: str) -> Backend:
    return _BACKENDS[backend]
