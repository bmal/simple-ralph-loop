"""Backend-agnostic Trust boundary checks shared by every Backend adapter.

Invariants:
- ``common_preflight`` is the platform-and-access floor every Iteration must clear
  before budget is spent: macOS only, the absolute-path ``caffeinate`` present,
  ``gh`` and the backend executable on PATH, a sanitized environment (no LLM API
  credential or ambiguous config-dir override), and a ``gh`` auth/repo proof that
  the accessible GitHub repository matches ``origin``.
- ``version_tuple`` parses a backend version string and fails closed if no
  ``N.N.N`` triple is present, so a version gate can never silently pass on
  unparseable output.

Depends on / must not know: ``errors``, ``launch`` (``caffeinate_executable``),
``environment`` (``reject_unsafe_environment``), and ``gitcontext`` (``command``,
``github_slug``). It must not know any backend-specific proof — those live in each
adapter and call these helpers.

See also: ``backends.opencode`` / ``backends.claude`` (call ``common_preflight``
and ``version_tuple`` before their own proofs).
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import sys

from .environment import reject_unsafe_environment
from .errors import RalphError
from .gitcontext import command, github_slug
from .launch import caffeinate_executable


def version_tuple(value: str, program: str = "OpenCode") -> tuple[int, int, int]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        raise RalphError(f"could not determine {program} version")
    return tuple(int(item) for item in match.groups())  # type: ignore[return-value]


def common_preflight(worktree: Path, slug: str, executable: str, env: dict[str, str]) -> None:
    if sys.platform != "darwin":
        raise RalphError("Ralph supports macOS only")
    if not Path(caffeinate_executable()).is_file():
        raise RalphError("/usr/bin/caffeinate is required")
    for required in ("gh", executable):
        if shutil.which(required) is None:
            raise RalphError(f"{required} is required")

    reject_unsafe_environment()
    command(["gh", "auth", "status"], cwd=worktree, env=env)
    repo = command(["gh", "repo", "view", slug, "--json", "url"], cwd=worktree, env=env)
    try:
        url = json.loads(repo.stdout)["url"]
    except (json.JSONDecodeError, KeyError, TypeError):
        raise RalphError("gh returned malformed repository information") from None
    if github_slug(url) != slug:
        raise RalphError("origin does not match the accessible GitHub repository")
