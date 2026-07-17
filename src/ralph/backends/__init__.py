"""The Backend package: per-backend default models, the Backend Protocol contract,
and the registry that resolves a backend name to its adapter module.

Invariants:
- ``DEFAULT_MODELS`` names the model each Backend runs when ``--model`` is omitted;
  the announced routing reflects it so a run always states what it will spend on.
- ``resolve`` is the single place a backend name becomes a Backend: it maps the name
  to the adapter module exactly once per invocation (register E1/E8). No
  ``backend == ...`` dispatch selecting an adapter's behavior survives anywhere
  else; the loop and cli drive the resolved Backend only through the five Protocol
  names and so cannot tell the two backends apart (register E2, user story 6).
- ``Backend`` pins those five names — ``preflight``, ``validate_model``,
  ``execute_iteration``, ``resume_argv``, ``environment`` — for type-checkers only;
  the adapters are plain modules, matched structurally with no runtime class or ABC
  machinery (register E1).

Depends on / must not know: the two adapter modules (imported so the registry can
name them). It must not grow backend-specific logic — that belongs in the adapters.

See also: ``backends.opencode`` / ``backends.claude`` (the adapters), ``loop`` and
``cli`` (resolve once, then drive the Backend through the five names), ``launch``
(the wrapped argv the adapters obtain).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from . import claude, opencode


DEFAULT_MODELS = {
    "claude": "claude-opus-4-8",
    "opencode": "openai/gpt-5.6-sol",
}


class Backend(Protocol):
    """The five-name Backend interface (register E2). Everything else an adapter
    does — event accumulation, iteration consumption, session persistence,
    OpenCode's second-pass verification — is adapter-private and invisible here."""

    def preflight(
        self, worktree: Path, slug: str, model: str, env: dict[str, str], allow_agents: bool = ...
    ) -> None: ...

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
    ) -> tuple[str, str | None]: ...

    def resume_argv(self, worktree: Path, model: str, session: str) -> list[str]: ...

    def environment(self, model: str) -> dict[str, str]: ...


_BACKENDS: dict[str, Backend] = {"claude": claude, "opencode": opencode}


def resolve(backend: str) -> Backend:
    return _BACKENDS[backend]
