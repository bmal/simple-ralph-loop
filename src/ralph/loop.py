"""The budgeted Iteration loop: per-run setup, handoff printing, and outcome
recording under the loop-wide power assertion.

Invariants:
- The loop-wide ``CaffeinateAssertion`` wraps the whole run and is re-checked with
  ``ensure_alive`` before every fresh session, so a lost power assertion stops the
  loop with retained evidence rather than continuing unprotected.
- Every terminal path writes ``outcome.json``, records the final git state, and ends
  with a Run console summary (register G9): a clean finish, a ``HandoffError``
  (resumable, records the session and prints a resume command), a
  ``StartedIterationError`` (slot consumed, nothing to resume), and an unexpected
  ``RalphError`` (recorded as ``backend_failure``, summarised, then re-raised for the
  top-level one-line handler). ``record_final_git_state`` returns the
  ``(branch, status)`` the summary is worded from and no longer prints the branch
  change; the console words the git outcome — branch, dirty state, and whether the
  branch's commits reached its upstream — from those facts (register G14).
- The loop spends exactly one backend session per iteration: an iteration that
  starts a session consumes its budget slot whatever the outcome, and the loop
  never restarts a slot itself. Any future retry allowance must reset per
  iteration — iterations are independent by design and must not accumulate state
  across one another.
- ``print_handoff`` reproduces the exact remaining-budget restart command and, when
  a session exists, the resume command; every operator-facing string is redacted. It
  no longer rings the bell — the Run console summary does, once per run, on every
  terminal outcome (register G12) — and the summary precedes it so the
  ``RALPH NEEDS OPERATOR`` block stays the last, most visible lines.
- The loop holds a resolved ``Backend`` and drives it only through the Backend
  Protocol (here ``environment``, ``preflight``, and ``execute_iteration``); it never
  names a concrete backend, so it cannot tell the two apart (register E2, user story
  6). Host isolation is established once per run through ``launch.establish_sandbox``,
  the shared fail-closed gate that generates the profile and proves it bites via
  the self-test before the first iteration (register D8) — or, under
  ``--unsafe-no-sandbox``, returns no profile so the backend runs unconfined
  (register D7). The loop threads ``args.unsafe_no_sandbox`` into that gate and
  into ``print_handoff`` so recovery reproduces the opt-out, and governs nothing
  else about host isolation.
- The concrete interactive-only children are resolved once per run, after the first
  iteration's preflight has proven the shared ``gh`` dependency and before that
  session spends budget: the loop asks ``gitcontext.interactive_only_issues`` for
  the open issues carrying the configured label, publishes the Loop protocol
  enriched with their numbers through ``set_active_protocol``, and retains the
  resolved set under ``interactive-only.json``. A failed or malformed query fails
  the run closed like any other preflight proof. The resolution is advisory --
  Ralph cannot observe which child the Backend selects -- so only the resulting
  needs-input halt is mechanical.
- The run's facts go to the injected ``RunConsole`` as value objects, never as text
  the loop worded itself (register G14): ``RunSettings`` for the header, an
  ``IterationOutcome`` for each Iteration's outcome block, and a ``RunSummary`` for
  the terminal summary. The header is emitted once the run directory exists and
  before host isolation is established, so the evidence path is on screen before any
  budget is spent or any failure reported. Each Iteration opens with a console rule
  and closes with its outcome block; the Trust boundary and the resolved
  interactive-only children complete the header from where their proof and resolution
  finish, necessarily after the first Iteration's rule (register G8). The only
  ``print`` still held here is the ``print_handoff`` banner, which a later ticket in
  the Run console program migrates; the structural test names it explicitly.

Depends on / must not know: ``console`` (the ``RunConsole`` abstraction and its
value objects -- never a concrete renderer), ``redaction`` (functions only),
``locking``, ``gitcontext``, ``protocol`` (``build_protocol`` /
``set_active_protocol``), ``launch``, ``errors``, and a resolved ``Backend``
(``cli`` resolves it through the registry and passes it in). It must not know which
concrete Backend it holds, nor how that Backend consumes the argv or produces its
events, nor how the Run console words or paints anything it is handed.

See also: ``console`` (the Run console and its rendering apparatus), ``launch``
(owns the wrapped argv, the profile gate, and recovery-command formatting), ``cli``
(``run`` resolves the Backend, acquires the lock, constructs the console, then
calls ``run_locked``), ``backends`` (the registry and Protocol).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sys
from typing import Any
import uuid

from .backends import Backend
from .console import IterationOutcome, RunConsole, RunSettings, RunSummary
from .errors import (
    HandoffError,
    RalphError,
    StartedIterationError,
)
from .gitcontext import command, interactive_only_issues, write_json
from .launch import (
    CaffeinateAssertion,
    establish_sandbox,
    resume_command,
    restart_command,
)
from .locking import secure_state_directory
from .protocol import build_protocol, set_active_protocol
from .redaction import collect_secrets, redact, set_active_redactor


def record_final_git_state(worktree: Path, run_dir: Path) -> tuple[str, str]:
    """Record the final git state as evidence and return the ``(branch, status)`` the
    run summary is worded from. The branch change, dirty state, and push state are
    facts the Run console words (register G14); this function no longer prints them."""
    branch_result = command(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"], cwd=worktree, allow_failure=True
    )
    final_branch = branch_result.stdout.strip() or "(detached)"
    status_result = command(
        ["git", "status", "--porcelain=v1", "--branch"], cwd=worktree, allow_failure=True
    )
    status = status_result.stdout or status_result.stderr
    (run_dir / "git-status-final.txt").write_text(status, encoding="utf-8")
    return final_branch, status


def build_summary(
    outcome: str, run_dir: Path, initial_branch: str, final_branch: str, status: str
) -> RunSummary:
    """Turn the recorded git state into the summary value object the Run console
    renders on a terminal outcome. The porcelain branch header carries the tracking
    upstream and how many commits are ahead of it, which is how the summary states
    whether the run's work was pushed."""
    dirty = any(line and not line.startswith("##") for line in status.splitlines())
    upstream: str | None = None
    ahead = 0
    for line in status.splitlines():
        if line.startswith("## "):
            header = line[3:]
            matched = re.search(r"\.\.\.(\S+)", header)
            upstream = matched.group(1) if matched else None
            ahead_match = re.search(r"\[ahead (\d+)", header)
            ahead = int(ahead_match.group(1)) if ahead_match else 0
            break
    return RunSummary(
        outcome=outcome,
        run_dir=run_dir,
        initial_branch=initial_branch,
        final_branch=final_branch,
        dirty=dirty,
        upstream=upstream,
        ahead=ahead,
    )


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
    no_sandbox: bool = False,
) -> None:
    terminal = sys.stderr.isatty()
    if terminal:
        # The bell now rings once per run in the Run console's summary (register
        # G12), on every terminal outcome rather than only the handoff, so it is no
        # longer emitted here; the handoff keeps only its failure-role colour.
        print("\033[1;31m", end="", file=sys.stderr)
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
            "manual resume: "
            f"{resume_command(backend, model, worktree, session_id, allow_agents, no_sandbox)}",
            file=sys.stderr,
        )
    print(f"iterations remaining: {remaining}", file=sys.stderr)
    if remaining:
        print(
            "continue Ralph: "
            f"{restart_command(backend, model, worktree, prompt_path, remaining, timeout, allow_agents, no_sandbox)}",
            file=sys.stderr,
        )
    else:
        print("No iterations remain to continue Ralph.", file=sys.stderr)
    print("==========================================", end="", file=sys.stderr)
    print("\033[0m" if terminal else "", file=sys.stderr)


def run_locked(
    backend: Backend,
    args: argparse.Namespace,
    prompt_path: Path,
    prompt: str,
    worktree: Path,
    git_dir: Path,
    branch: str,
    status: str,
    slug: str,
    console: RunConsole,
) -> int:
    with CaffeinateAssertion(worktree) as assertion:
        return run_protected(
            backend,
            args,
            prompt_path,
            prompt,
            worktree,
            git_dir,
            branch,
            status,
            slug,
            assertion,
            console,
        )


def run_protected(
    backend: Backend,
    args: argparse.Namespace,
    prompt_path: Path,
    prompt: str,
    worktree: Path,
    git_dir: Path,
    branch: str,
    status: str,
    slug: str,
    assertion: CaffeinateAssertion,
    console: RunConsole,
) -> int:
    env = backend.environment(args.model)
    # Redact subscription credentials from every readable and retained stream in
    # case backend output echoes an environment value. This precedes the header so
    # the Run console's choke point already has a live redactor to scrub through.
    set_active_redactor(collect_secrets())

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
    # Open the run with the header: the resolved settings the loop is about to
    # spend budget on and the evidence path they will be recorded under (register
    # G8). It is emitted the moment the run directory exists and before host
    # isolation is established, so no budget is spent — and no failure reported —
    # before the operator has been told what this run is and where to look.
    console.run_started(
        RunSettings(
            backend=args.backend,
            model=args.model,
            iterations=args.iterations,
            timeout=args.timeout,
            repository=slug,
            branch=branch,
            worktree=worktree,
            prompt_path=prompt_path,
            interactive_label=args.interactive_label,
            run_dir=run_dir,
            dirty=any(
                line and not line.startswith("##") for line in status.splitlines()
            ),
        )
    )
    # Establish host isolation once per run (the profile is stable across
    # iterations): generate the per-run profile and prove it actually bites via
    # the one-shot self-test before any budget is spent, or stop fail-closed here
    # (register D2/D6/D8) — exactly as the caffeinate startup assertion gates the
    # whole loop. Both backends are wrapped uniformly (#20 OpenCode, #22 Claude);
    # `--unsafe-no-sandbox` relaxes only this, returning no profile so the shared
    # Launch chain runs the backend unconfined (register D7).
    sandbox_profile = establish_sandbox(
        args.backend,
        run_dir,
        worktree,
        git_dir / "ralph",
        env,
        no_sandbox=args.unsafe_no_sandbox,
    )
    started = datetime.now(timezone.utc).isoformat()
    iterations: list[dict[str, Any]] = []
    session_id: str | None = None
    outcome = "budget_exhausted"
    # The slot in flight, kept current for the handoff handlers so a started
    # iteration is charged against its own number.
    number = 0
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
            # Open the Iteration with a rule naming its number and the budget; the
            # Run console words it, so multi-iteration output stays attributable to a
            # specific fresh session (register G2/G9).
            console.iteration_started(number, args.iterations)
            backend.preflight(worktree, slug, args.model, env, args.unsafe_allow_agents)
            if number == 1:
                # The full Trust boundary is proven once this first preflight has
                # cleared: subscription-only authentication and customization
                # isolation here, host isolation already proven by the sandbox
                # self-test before the loop (omitted, and so not claimed, under
                # --unsafe-no-sandbox). It prints where its proof completes, after the
                # first Iteration's banner, rather than in the header (register G8).
                properties = ["subscription-only authentication", "customization isolation"]
                if not args.unsafe_no_sandbox:
                    properties.append("host isolation")
                console.trust_boundary_established(properties)
                # Resolve the concrete interactive-only children once per run, now
                # that preflight has proven the shared gh dependency, and publish
                # the enriched protocol before this first session spends budget; a
                # failed or malformed query fails the run closed here. The resolved
                # set is retained so the run's evidence records what was resolved.
                interactive_only = interactive_only_issues(
                    slug, args.interactive_label, worktree, env
                )
                set_active_protocol(
                    build_protocol(args.interactive_label, interactive_only)
                )
                write_json(
                    run_dir / "interactive-only.json",
                    {"label": args.interactive_label, "issues": interactive_only},
                )
                # Completes the header where its resolution finishes: the concrete
                # children cannot be known until preflight has proven gh, so this
                # line lands after the first iteration's rule for the same reason
                # register G8 lands the Trust boundary line there.
                console.interactive_only_resolved(args.interactive_label, interactive_only)
            iteration_started_at = datetime.now(timezone.utc)
            iteration_started = iteration_started_at.isoformat()
            # A started session consumes its slot whatever the outcome; the loop
            # never restarts a slot itself.
            outcome, session_id, concluding_message = backend.execute_iteration(
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
            # Close the Iteration with its outcome block: duration, outcome, session
            # id, and the Backend's concluding message truncated for display. The raw
            # message is a fact the console words; nothing is written to disk from the
            # truncation, so the retained artifacts stay byte-identical (register G18).
            console.iteration_finished(
                IterationOutcome(
                    number=number,
                    iterations=args.iterations,
                    duration_seconds=(
                        datetime.now(timezone.utc) - iteration_started_at
                    ).total_seconds(),
                    outcome=outcome,
                    session_id=session_id,
                    concluding_message=concluding_message,
                )
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
        final_branch, status = record_final_git_state(worktree, run_dir)
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
        # The summary precedes the handoff banner so the RALPH NEEDS OPERATOR block
        # and its resume command stay the last, most visible lines; the summary rings
        # the bell for this terminal outcome (register G9/G12).
        console.run_finished(build_summary(outcome, run_dir, branch, final_branch, status))
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
            no_sandbox=args.unsafe_no_sandbox,
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
        final_branch, status = record_final_git_state(worktree, run_dir)
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
        console.run_finished(build_summary(outcome, run_dir, branch, final_branch, status))
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
            no_sandbox=args.unsafe_no_sandbox,
        )
        return 2
    except RalphError:
        final_branch, status = record_final_git_state(worktree, run_dir)
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
        # The summary states the git outcome and evidence path on the error path too;
        # the one-line ``ralph: <message>`` still follows from the top-level handler.
        console.run_finished(
            build_summary("backend_failure", run_dir, branch, final_branch, status)
        )
        raise
    final_branch, status = record_final_git_state(worktree, run_dir)
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
    # Every run ends with a summary, including a successful one that used to exit
    # silently: the git outcome and the evidence path, and the bell (register G9).
    # The budget-exhausted headline keeps the byte-identical "iteration budget
    # exhausted" phrase an operator greps for (register G19).
    console.run_finished(build_summary(outcome, run_dir, branch, final_branch, status))
    return 0 if outcome == "complete" else 1
