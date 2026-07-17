"""The Backend package: per-backend default models and the transitional dispatch
over the two Backend adapters.

Invariants:
- ``DEFAULT_MODELS`` names the model each Backend runs when ``--model`` is omitted;
  the announced routing reflects it so a run always states what it will spend on.
- ``execute_iteration`` and ``validate_model`` still branch on the backend name
  here. This is deliberate for register E8 commit 1 (re-homing): the dispatch is
  preserved, not activated. Commit 2 replaces these branches with a single registry
  resolution and moves ``validate_model`` behind each adapter's Protocol contract.

Depends on / must not know: ``errors`` and the two adapter modules. It must not
grow backend-specific logic — that belongs in the adapters.

See also: ``backends.opencode`` / ``backends.claude`` (the adapters), ``loop``
and ``cli`` (dispatch through these functions).
"""

from __future__ import annotations

from pathlib import Path

from ..errors import RalphError
from .claude import execute_claude_iteration
from .opencode import execute_opencode_iteration


DEFAULT_MODELS = {
    "claude": "claude-opus-4-8",
    "opencode": "openai/gpt-5.6-sol",
}


def execute_iteration(
    backend: str,
    worktree: Path,
    run_dir: Path,
    prompt: str,
    model: str,
    env: dict[str, str],
    timeout: float,
    sandbox_profile: Path | None = None,
) -> tuple[str, str | None]:
    if backend == "claude":
        # The Claude wrap lands in #22; until then the shared profile is carried
        # but only the OpenCode launch consumes it.
        return execute_claude_iteration(worktree, run_dir, prompt, model, env, timeout)
    return execute_opencode_iteration(
        worktree, run_dir, prompt, model, env, timeout, sandbox_profile
    )


def validate_model(backend: str, model: str) -> None:
    if backend == "opencode" and (not model.startswith("openai/") or model == "openai/"):
        raise RalphError("model must use the openai/ provider")
    if backend == "claude" and not model.startswith("claude-"):
        raise RalphError("model must be a Claude subscription model")
