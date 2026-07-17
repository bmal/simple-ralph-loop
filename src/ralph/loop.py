"""The budgeted Iteration loop: per-run setup, handoff printing, and outcome
recording under the loop-wide power assertion.

Invariants:
- The loop-wide ``CaffeinateAssertion`` wraps the whole run and is re-checked with
  ``ensure_alive`` before every fresh session, so a lost power assertion stops the
  loop with retained evidence rather than continuing unprotected.
- Every terminal path writes ``outcome.json`` and records the final git state: a
  clean finish, a ``HandoffError`` (resumable, records the session and prints a
  resume command), a ``StartedIterationError`` (slot consumed, nothing to resume),
  and an unexpected ``RalphError`` (recorded as ``backend_failure`` then re-raised).
- ``print_handoff`` reproduces the exact remaining-budget restart command and, when
  a session exists, the resume command; every operator-facing string is redacted.
- The per-iteration preflight branch and the sandbox-profile-for-opencode branch
  are transitional dispatch (register E8 commit 1); the launch-chain seam and the
  registry absorb them in commit 2.

Depends on / must not know: ``environment``, ``redaction`` (functions only),
``locking``, ``gitcontext``, ``launch``, ``errors``, and the ``backends`` package
plus its two adapters (for the transitional preflight dispatch). It must not know
how any Backend consumes the argv or produces its events.

See also: ``launch`` (owns the wrapped argv and recovery-command formatting),
``cli`` (``run`` acquires the lock then calls ``run_locked``), ``backends``.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
from typing import Any
import uuid

from .backends import execute_iteration
from .backends.claude import claude_preflight
from .backends.opencode import opencode_preflight
from .environment import clean_environment
from .errors import HandoffError, RalphError, StartedIterationError
from .gitcontext import command, write_json
from .launch import (
    CaffeinateAssertion,
    resume_command,
    restart_command,
    write_sandbox_profile,
)
from .locking import secure_state_directory
from .redaction import collect_secrets, redact, set_active_redactor


def record_final_git_state(worktree: Path, run_dir: Path, initial_branch: str) -> str:
    branch_result = command(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"], cwd=worktree, allow_failure=True
    )
    final_branch = branch_result.stdout.strip() or "(detached)"
    status_result = command(
        ["git", "status", "--porcelain=v1", "--branch"], cwd=worktree, allow_failure=True
    )
    status = status_result.stdout or status_result.stderr
    (run_dir / "git-status-final.txt").write_text(status, encoding="utf-8")
    if final_branch != initial_branch:
        print(
            f"ralph: warning: branch changed from {initial_branch} to {final_branch}",
            file=sys.stderr,
        )
    return final_branch


def print_handoff(
    *,
    reason: str,
    session_id: str | None,
    detail: str | None,
    backend: str,
    model: str,
    worktree: Path,
    prompt_path: Path,
    remaining: int,
    run_id: str,
    timeout: float,
    allow_agents: bool = False,
) -> None:
    terminal = sys.stderr.isatty()
    if terminal:
        print("\a\033[1;31m", end="", file=sys.stderr)
    print("========== RALPH NEEDS OPERATOR ==========", file=sys.stderr)
    print(f"reason: {redact(reason)}", file=sys.stderr)
    print(f"ralph run: {run_id}", file=sys.stderr)
    if session_id:
        print(f"{backend} session: {session_id}", file=sys.stderr)
    if detail:
        print(f"question/error: {redact(detail)}", file=sys.stderr)
    if session_id:
        # Without a session id there is nothing to resume; the operator handoff
        # still prints the remaining-budget command so the loop can continue.
        print(
            f"manual resume: {resume_command(backend, model, worktree, session_id, allow_agents)}",
            file=sys.stderr,
        )
    print(f"iterations remaining: {remaining}", file=sys.stderr)
    if remaining:
        print(
            "continue Ralph: "
            f"{restart_command(backend, model, worktree, prompt_path, remaining, timeout, allow_agents)}",
            file=sys.stderr,
        )
    else:
        print("No automatic replacement iteration remains.", file=sys.stderr)
    print("==========================================", end="", file=sys.stderr)
    print("\033[0m" if terminal else "", file=sys.stderr)


def run_locked(
    args: argparse.Namespace,
    prompt_path: Path,
    prompt: str,
    worktree: Path,
    git_dir: Path,
    branch: str,
    status: str,
    slug: str,
) -> int:
    with CaffeinateAssertion(worktree) as assertion:
        return run_protected(
            args, prompt_path, prompt, worktree, git_dir, branch, status, slug, assertion
        )


def run_protected(
    args: argparse.Namespace,
    prompt_path: Path,
    prompt: str,
    worktree: Path,
    git_dir: Path,
    branch: str,
    status: str,
    slug: str,
    assertion: CaffeinateAssertion,
) -> int:
    # Announce the resolved routing up front so a run's console output states
    # exactly which backend and model the loop is about to spend budget on,
    # including when the model came from DEFAULT_MODELS rather than --model.
    print(f"ralph: backend {args.backend}, model {args.model}", file=sys.stderr)
    if any(line and not line.startswith("##") for line in status.splitlines()):
        print("ralph: warning: worktree has uncommitted changes", file=sys.stderr)
    env = clean_environment(args.model, args.backend)
    # Redact subscription credentials from every readable and retained stream in
    # case backend output echoes an environment value.
    set_active_redactor(collect_secrets())
    print(
        "WARNING: Ralph always uses dangerous full-auto mode permissions; the backend may edit files "
        "and run commands without confirmation.",
        file=sys.stderr,
    )
    print(
        "WARNING: caffeinate cannot prevent lid-close or explicit sleep, power loss, or external "
        "network and service outages.",
        file=sys.stderr,
    )

    runs_root = secure_state_directory(git_dir, "ralph", "runs")
    run_dir = runs_root / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex[:8]
    )
    try:
        os.mkdir(run_dir)
    except FileExistsError:
        raise RalphError("Ralph run directory already exists") from None
    (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    write_json(
        run_dir / "options.json",
        {
            "backend": args.backend,
            "branch": branch,
            "iterations": args.iterations,
            "model": args.model,
            "prompt": str(prompt_path),
            "repository": slug,
            "timeout": args.timeout,
            "worktree": str(worktree),
        },
    )
    (run_dir / "git-status.txt").write_text(status, encoding="utf-8")
    # Generate the host-isolation profile once per run (stable across
    # iterations) and confine the backend under it (register D2/D6). #20 wraps
    # OpenCode; the Claude wrap (#22) and the --unsafe-no-sandbox opt-out with
    # its fail-closed self-test (#21/#23) build on this shared launcher.
    sandbox_profile: Path | None = None
    if args.backend == "opencode":
        sandbox_profile = write_sandbox_profile(
            run_dir, args.backend, worktree, git_dir / "ralph", env
        )
    started = datetime.now(timezone.utc).isoformat()
    iterations: list[dict[str, Any]] = []
    session_id: str | None = None
    outcome = "budget_exhausted"
    try:
        for number in range(1, args.iterations + 1):
            # The loop-wide sleep assertion must still be held before each fresh
            # session; a lost assertion stops the loop with retained evidence.
            assertion.ensure_alive()
            iteration_dir = (
                run_dir
                if number == 1
                else secure_state_directory(run_dir, f"iteration-{number:03d}")
            )
            # Mark the boundary between fresh sessions so multi-iteration
            # console output is attributable to a specific iteration.
            print(f"ralph: iteration {number} of {args.iterations}", file=sys.stderr)
            if args.backend == "claude":
                claude_preflight(worktree, slug, args.model, env, args.unsafe_allow_agents)
            else:
                opencode_preflight(worktree, slug, args.model, env, args.unsafe_allow_agents)
            iteration_started = datetime.now(timezone.utc).isoformat()
            outcome, session_id = execute_iteration(
                args.backend,
                worktree,
                iteration_dir,
                prompt,
                args.model,
                env,
                args.timeout,
                sandbox_profile,
            )
            iterations.append(
                {
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "number": number,
                    "outcome": outcome,
                    "session_id": session_id,
                    "started_at": iteration_started,
                }
            )
            if outcome == "complete":
                break
    except HandoffError as error:
        iterations.append(
            {
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "number": number,
                "outcome": error.outcome,
                "reason": error.reason,
                "session_id": error.session_id,
                "started_at": iteration_started,
            }
        )
        outcome = error.outcome
        final_branch = record_final_git_state(worktree, run_dir, branch)
        write_json(
            run_dir / "outcome.json",
            {
                "final_branch": final_branch,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "iterations": iterations,
                "outcome": outcome,
                "session_id": error.session_id,
                "started_at": started,
            },
        )
        print_handoff(
            reason=error.reason,
            session_id=error.session_id,
            detail=error.detail,
            backend=args.backend,
            model=args.model,
            worktree=worktree,
            prompt_path=prompt_path,
            remaining=args.iterations - number,
            run_id=run_dir.name,
            timeout=args.timeout,
            allow_agents=args.unsafe_allow_agents,
        )
        return 2
    except StartedIterationError as error:
        # A started iteration that stopped before any session metadata still
        # consumes its slot. There is no session to resume, but the operator
        # handoff must appear with the exact remaining-budget command.
        iterations.append(
            {
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "number": number,
                "outcome": error.outcome,
                "reason": error.reason,
                "session_id": None,
                "started_at": iteration_started,
            }
        )
        outcome = error.outcome
        final_branch = record_final_git_state(worktree, run_dir, branch)
        write_json(
            run_dir / "outcome.json",
            {
                "final_branch": final_branch,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "iterations": iterations,
                "outcome": outcome,
                "session_id": None,
                "started_at": started,
            },
        )
        print_handoff(
            reason=error.reason,
            session_id=None,
            detail=None,
            backend=args.backend,
            model=args.model,
            worktree=worktree,
            prompt_path=prompt_path,
            remaining=args.iterations - number,
            run_id=run_dir.name,
            timeout=args.timeout,
            allow_agents=args.unsafe_allow_agents,
        )
        return 2
    except RalphError:
        final_branch = record_final_git_state(worktree, run_dir, branch)
        write_json(
            run_dir / "outcome.json",
            {
                "final_branch": final_branch,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "iterations": iterations,
                "outcome": "backend_failure",
                "started_at": started,
            },
        )
        raise
    final_branch = record_final_git_state(worktree, run_dir, branch)
    write_json(
        run_dir / "outcome.json",
        {
            "final_branch": final_branch,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "iterations": iterations,
            "outcome": outcome,
            "session_id": session_id,
            "started_at": started,
        },
    )
    if outcome == "complete":
        return 0
    print("RALPH INCOMPLETE: iteration budget exhausted without completion", file=sys.stderr)
    return 1
