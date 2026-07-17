"""The subprocess helper, prompt reading, git/GitHub context resolution, and
redacted JSON artifact writing.

Invariants:
- ``command`` is the single subprocess entry point for preflight/git helpers: an
  ``OSError`` becomes a ``RalphError`` naming the executable, and a non-zero exit
  fails closed as a preflight failure unless ``allow_failure`` is set.
- ``read_prompt`` accepts only an existing, readable, regular file that is
  non-empty, no larger than ``MAX_PROMPT_BYTES`` (10 MiB), and valid UTF-8; every
  other case raises rather than feeding an unbounded or undecodable prompt.
- ``git_context`` refuses a detached HEAD and requires a GitHub ``origin``; a
  worktree's git-dir and branch are resolved by absolute path so downstream state
  lands under the worktree's private Git directory.
- ``write_json`` scrubs through ``redact`` before writing, so no retained artifact
  can carry a subscription credential echoed by the backend.

Depends on / must not know: ``errors`` and ``redaction`` (functions only, never
the active-redactor global). It must not know which command it is running beyond
building the subprocess.

See also: ``locking`` (writes lock metadata via ``write_json``), ``preflight``
and the Backend adapters (run ``command`` for their proofs), ``loop`` (writes run
artifacts).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any
from urllib.parse import urlparse

from .errors import RalphError
from .redaction import redact


MAX_PROMPT_BYTES = 10 * 1024 * 1024


def command(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True)
    except OSError as error:
        raise RalphError(f"cannot run {args[0]}: {error.strerror}") from None
    if result.returncode and not allow_failure:
        raise RalphError(f"{args[0]} preflight failed")
    return result


def read_prompt(path_text: str) -> tuple[Path, str]:
    try:
        path = Path(path_text).expanduser().resolve(strict=True)
    except OSError:
        raise RalphError("prompt file does not exist") from None
    if not path.is_file() or not os.access(path, os.R_OK):
        raise RalphError("prompt must be a readable regular file")
    size = path.stat().st_size
    if size == 0 or size > MAX_PROMPT_BYTES:
        raise RalphError("prompt must be non-empty and no larger than 10 MiB")
    try:
        return path, path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise RalphError("prompt must be valid UTF-8") from None


def github_slug(remote: str) -> str:
    remote = remote.strip()
    ssh = re.fullmatch(r"git@github\.com:([^/]+/[^/]+?)(?:\.git)?", remote)
    if ssh:
        return ssh.group(1)
    parsed = urlparse(remote)
    if parsed.scheme in {"https", "ssh"} and parsed.hostname == "github.com":
        slug = parsed.path.strip("/")
        if slug.endswith(".git"):
            slug = slug[:-4]
        if slug.count("/") == 1:
            return slug
    raise RalphError("origin must be a GitHub repository")


def git_context(worktree_text: str | None) -> tuple[Path, Path, str, str, str]:
    requested = Path(worktree_text or os.getcwd()).expanduser().resolve()
    if not requested.is_dir():
        raise RalphError("worktree is not a directory")
    top = Path(command(["git", "rev-parse", "--show-toplevel"], cwd=requested).stdout.strip()).resolve()
    branch_result = command(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"], cwd=top, allow_failure=True
    )
    branch = branch_result.stdout.strip()
    if branch_result.returncode or not branch:
        raise RalphError("detached HEAD is not supported")
    git_dir_text = command(["git", "rev-parse", "--path-format=absolute", "--git-dir"], cwd=top).stdout.strip()
    git_dir = Path(git_dir_text).resolve()
    remote = command(["git", "remote", "get-url", "origin"], cwd=top).stdout.strip()
    status = command(["git", "status", "--porcelain=v1", "--branch"], cwd=top).stdout
    return top, git_dir, branch, status, github_slug(remote)


def write_json(path: Path, value: Any) -> None:
    path.write_text(redact(json.dumps(value, indent=2, sort_keys=True)) + "\n", encoding="utf-8")
