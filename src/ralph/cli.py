"""The Ralph command-line entry point: argument parsing and the run/clean/resume
commands.

Invariants:
- ``run`` validates the iteration budget (1..100), the timeout (finite, zero or
  positive, at most ``MAX_ITERATION_TIMEOUT_SECONDS``), and the model before any
  budget is spent, resolves the default model per backend, then acquires the
  worktree lock and hands off to the Loop.
- ``clean`` removes only a real ``.git/ralph`` state directory, never following a
  symlink or deleting an unexpected file type, and refuses while a live loop holds
  the worktree lock.
- ``resume`` re-establishes the full Trust boundary (sanitized environment,
  per-session OAuth/routing proof, isolated configuration, full-auto permissions,
  caffeinate) before ``exec``-ing the interactive backend, so recovery can never
  inherit unsafe ambient routing. It resolves the Backend through the registry and
  obtains its wrapped argv from ``launch.session_argv``, the one seam #19 edits.
- ``main`` is the single place a ``RalphError`` becomes ``ralph: <message>`` on
  stderr with exit code 2; the console script and ``python -m ralph.cli`` both run
  it, and the name ``main`` is preserved for the packaging entry point.

Depends on / must not know: the ``backends`` package (defaults, the registry, and
the resolved Backend's five interface names), ``redaction`` (functions only),
``gitcontext``, ``launch`` (``session_argv``), ``locking``, ``loop``, ``process``
(timeout ceiling), and ``errors``. It resolves the Backend once and drives it only
through the interface; it must not contain any Backend, Launch chain, or Loop
mechanism of its own, nor branch on the backend name.

See also: ``loop`` (the budgeted Iteration loop), ``backends`` (the registry and
adapters), ``launch`` (wrapped argv and recovery-command formatting), package
docstring in ``ralph`` (the map).
"""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import shutil
import stat
import sys

from .backends import DEFAULT_MODELS, resolve
from .errors import RalphError
from .gitcontext import command, git_context, read_prompt
from .launch import session_argv
from .locking import WorktreeLock
from .loop import run_locked
from .process import MAX_ITERATION_TIMEOUT_SECONDS
from .redaction import collect_secrets, set_active_redactor


def run(args: argparse.Namespace) -> int:
    if not 1 <= args.iterations <= 100:
        raise RalphError("iterations must be between 1 and 100")
    if not math.isfinite(args.timeout) or args.timeout < 0:
        raise RalphError("timeout must be zero or positive and finite")
    if args.timeout > MAX_ITERATION_TIMEOUT_SECONDS:
        raise RalphError(
            f"timeout must not exceed {MAX_ITERATION_TIMEOUT_SECONDS} seconds so backend "
            "request and Bash limits stay subordinate to Ralph's timer"
        )
    backend = resolve(args.backend)
    args.model = args.model or DEFAULT_MODELS[args.backend]
    backend.validate_model(args.model)

    prompt_path, prompt = read_prompt(args.prompt)
    worktree, git_dir, branch, status, slug = git_context(args.worktree)
    with WorktreeLock(git_dir, git_dir / "ralph" / "lock.json"):
        return run_locked(
            backend, args, prompt_path, prompt, worktree, git_dir, branch, status, slug
        )


def clean(args: argparse.Namespace) -> int:
    requested = Path(args.worktree or os.getcwd()).expanduser().resolve()
    if not requested.is_dir():
        raise RalphError("worktree is not a directory")
    top = Path(command(["git", "rev-parse", "--show-toplevel"], cwd=requested).stdout.strip()).resolve()
    git_dir = Path(
        command(["git", "rev-parse", "--path-format=absolute", "--git-dir"], cwd=top).stdout.strip()
    ).resolve()
    state_root = git_dir / "ralph"
    # Refuse while a live loop holds the worktree lock so active logs and locks
    # cannot disappear underneath the process.
    lock = WorktreeLock(git_dir)
    lock.acquire()
    try:
        try:
            info = os.lstat(state_root)
        except FileNotFoundError:
            return 0
        # Never follow a symlink or delete an unexpected file type: only a real
        # Ralph state directory is removed, and shutil.rmtree does not follow
        # symlinked children, so backend transcripts and source files outside
        # .git/ralph are never touched.
        if stat.S_ISLNK(info.st_mode):
            raise RalphError("refusing to remove a symlinked Ralph state path")
        if not stat.S_ISDIR(info.st_mode):
            raise RalphError("Ralph state path is not a directory")
        shutil.rmtree(state_root)
    finally:
        lock.release()
    return 0


def resume(args: argparse.Namespace) -> int:
    backend = resolve(args.backend)
    backend.validate_model(args.model)
    worktree, _git_dir, _branch, _status, slug = git_context(args.worktree)
    # Re-establish the exact sanitized child environment and re-prove the
    # subscription trust boundary (OAuth, effective routing, model availability,
    # customization isolation) before any resumed model work. reject_unsafe_-
    # environment inside preflight fails closed on a newly added API credential
    # or custom endpoint, so recovery cannot silently inherit unsafe routing.
    env = backend.environment(args.model)
    set_active_redactor(collect_secrets())
    backend.preflight(worktree, slug, args.model, env, args.unsafe_allow_agents)
    # The Launch chain assembles the wrapped argv: caffeinate outermost, launched
    # by absolute path exactly as automated iterations do (preflight has proved it
    # exists). Holding the -im assertion for the interactive session's whole
    # lifetime replaces Ralph's own loop-level assertion once control passes to the
    # operator.
    argv = session_argv(backend.resume_argv(worktree, args.model, args.session))
    print(
        "WARNING: Ralph is relaunching the backend session in dangerous full-auto mode; "
        "it may edit files and run commands without confirmation.",
        file=sys.stderr,
    )
    try:
        os.chdir(worktree)
        os.execvpe(argv[0], argv, env)
    except OSError as error:
        raise RalphError(f"could not launch {args.backend} for resume: {error.strerror}") from None
    return 2


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="ralph")
    subcommands = result.add_subparsers(dest="command", required=True)
    run_parser = subcommands.add_parser("run", help="run bounded coding-agent iterations")
    run_parser.add_argument("prompt")
    run_parser.add_argument("--backend", choices=["claude", "opencode"], required=True)
    run_parser.add_argument("--iterations", type=int, required=True)
    run_parser.add_argument("--model")
    run_parser.add_argument(
        "--timeout",
        type=float,
        default=3600,
        help=(
            "seconds allowed per iteration; zero disables the limit "
            f"(default: 3600, maximum: {MAX_ITERATION_TIMEOUT_SECONDS})"
        ),
    )
    run_parser.add_argument("--worktree")
    run_parser.add_argument(
        "--unsafe-allow-agents",
        action="store_true",
        help=(
            "allow the repo's backend agents instead of refusing them (Claude: "
            ".claude/agents and the settings.json 'agent' key; OpenCode: the "
            "effective configuration's agent map); Ralph then cannot prove "
            "agent isolation (unsafe)"
        ),
    )
    clean_parser = subcommands.add_parser("clean", help="remove Ralph state for a worktree")
    clean_parser.add_argument("--worktree")
    resume_parser = subcommands.add_parser(
        "resume", help="relaunch a handed-off session under Ralph's trust boundary"
    )
    resume_parser.add_argument("--backend", choices=["claude", "opencode"], required=True)
    resume_parser.add_argument("--model", required=True)
    resume_parser.add_argument("--session", required=True)
    resume_parser.add_argument("--worktree")
    resume_parser.add_argument(
        "--unsafe-allow-agents",
        action="store_true",
        help=(
            "allow the repo's backend agents instead of refusing them (Claude: "
            ".claude/agents and the settings.json 'agent' key; OpenCode: the "
            "effective configuration's agent map); Ralph then cannot prove "
            "agent isolation (unsafe)"
        ),
    )
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "run":
            return run(args)
        if args.command == "clean":
            return clean(args)
        if args.command == "resume":
            return resume(args)
    except RalphError as error:
        print(f"ralph: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
