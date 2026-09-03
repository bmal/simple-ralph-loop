"""The budgeted Iteration loop: per-run setup, the operator help a stop hands off,
and outcome recording under the loop-wide power assertion.

Invariants:
- The loop-wide ``CaffeinateAssertion`` wraps the whole run and is re-checked with
  ``ensure_alive`` before every fresh session, so a lost power assertion stops the
  loop with retained evidence rather than continuing unprotected. The check is told
  which session it guards, because an assertion already gone before the first one is
  a startup failure and one lost later is not, and only the loop knows which.
- An Iteration's outcome names what happened to *the Iteration*; the run-level word
  is this module's own decision (register J3). The adapters return ``complete`` or
  ``incomplete`` -- ending without a completion marker is a normal end of iteration
  under the Loop protocol, not a budget that ran out -- and ``budget_exhausted`` is
  decided here, once ``range(1, iterations + 1)`` is exhausted with nothing having
  completed. So the run-level value is never inherited from the last Iteration, and
  the byte-identical ``iteration budget exhausted`` phrase still greps once, at the
  summary (register J5).
- Every terminal path closes the Iteration in flight with its outcome block before
  the summary, the raising paths included (``close_iteration``): the Iteration that
  timed out, handed off, or violated the contract is the one an operator most wants
  attributed, and it used to get no block at all. Number, budget, duration and
  outcome always; the session id when the Iteration being closed established one --
  it is dropped as each Iteration is announced, so a pre-session failure says "no
  session id" rather than naming its predecessor's session; a concluding message only
  on the normal return, which is the only path that receives one. A bare ``RalphError``
  raised after a session existed reports none either, because that path hands the loop
  no session -- only the handoff errors carry one. The duration is measured
  from where the Iteration was announced rather than from where its session started,
  so one block means one thing however the Iteration ended. What closes is the
  Iteration the operator was told had started, and it closes once: a failure before
  the first rule closes none, and one *between* Iterations — a lost power assertion —
  closes none either, because the one before it already closed.
- Every terminal path writes ``outcome.json``, records the final git state, and ends
  with a Run console summary (register G9): a clean finish, a ``HandoffError``
  (resumable, records the session and the ``RALPH NEEDS OPERATOR`` help block with a
  resume command), a ``StartedIterationError`` (slot consumed, nothing to resume,
  same block without one), and a ``RalphError`` (recorded as ``backend_failure``,
  summarised, then given ``failure_help`` — the reason and a next step, never the
  handoff banner, because a pre-session failure is not resumable). Once a run
  directory exists every stop gets the full help block; the loop no longer re-raises
  the backend failure to a one-line handler (register G10). ``record_final_git_state``
  returns the ``(branch, status)`` the summary is worded from; the console words the
  git outcome — branch, dirty state, and whether the branch's commits reached its
  upstream — from those facts (register G14).
- This module names where a run's evidence sits *inside* the state root
  (``RUNS_DIRECTORY``) because it is the module that creates those directories.
  ``retained_runs`` counts them for ``clean``'s report (register G22), so the command
  that destroys them never has to know that layout — it knows only the state root it
  was already resolving. The count descends into nothing and follows no symlink, and a
  state root that never held a run counts zero rather than failing.
- The loop spends exactly one backend session per iteration: an iteration that
  starts a session consumes its budget slot whatever the outcome, and the loop
  never restarts a slot itself. Any future retry allowance must reset per
  iteration — iterations are independent by design and must not accumulate state
  across one another.
- ``handoff_help`` builds the full help block from the run's facts and the exact
  recovery commands the Launch chain produces — the remaining-budget restart and,
  when a session exists, the resume; the console words the block and redacts it. The
  summary precedes it and rings the bell once per run (register G12), so the
  ``RALPH NEEDS OPERATOR`` block stays the last, most visible lines. Budget exhaustion
  gains the continuation command through ``console.budget_continue`` at the clean end.
- The run's whole invocation is read once, into a ``launch.Invocation``, and every
  recovery command the run can print is built from that one reading. The loop
  decides only the budget a continuation is offered — what a handoff left, or the
  whole of it restored after exhaustion — and never which flags a command carries,
  so a handoff and budget exhaustion cannot disagree about the run they came from
  (they did: the exhaustion command dropped ``--in-scope-backend``, and neither
  carried ``--interactive-label``).
- The loop holds a resolved ``Backend`` and drives it only through the Backend
  Protocol (here ``environment``, ``preflight``, and ``execute_iteration``); it never
  names a concrete backend, so it cannot tell the two apart (register E2, user story
  6). Host isolation is established once per run through ``launch.establish_sandbox``,
  the shared fail-closed gate that generates the profile and proves it bites via
  the self-test before the first iteration (register D8) — or, under
  ``--unsafe-no-sandbox``, returns no profile so the backend runs unconfined
  (register D7). That gate is silent; the loop states the relaxed guarantees loudly
  through ``console.deviation`` — the sandbox opt-out from the flag it holds, and an
  admitted agent vector from the ``Deviation`` ``preflight`` hands back (register
  G7/G14). It threads ``args.unsafe_no_sandbox`` into the gate, and the run's
  ``Invocation`` carries it into the recovery commands so recovery reproduces the
  opt-out; it governs nothing else about host isolation.
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
  the terminal summary. The same console is injected into ``execute_iteration`` as the
  narrow Observation sink (register G15), so the Backend reports live progress through
  it while the loop is blocked in that call; the loop passes the console as an
  abstraction and never itself observes. A concluding message is passed through
  ``protocol.without_marker_lines`` on its way there, because the console owns no
  marker knowledge of its own and this module is where the protocol and the console
  meet: what it hands over is the Backend's prose, and the outcome beside it already
  reports what a marker signalled (the ratified owner decision on issue #58). That is
  display only -- the retained artifacts keep the whole message, markers included
  (register G18). The header is emitted once the run directory exists and
  before host isolation is established, so the evidence path is on screen before any
  budget is spent or any failure reported. Each Iteration opens with a console rule
  and closes with its outcome block; the Trust boundary and the resolved
  interactive-only children complete the header from where their proof and resolution
  finish, necessarily after the first Iteration's rule (register G8). The loop holds
  no ``print`` of its own: every operator-facing line — header, iteration blocks,
  summary, deviations, and the full help block — goes through the injected console
  (register G13). It reaches for no standard stream of its own under any spelling,
  which is the whole of what the structural rule asks and why that rule needs no
  allowlist of stragglers.

Depends on / must not know: ``console`` (the ``RunConsole`` abstraction and its
value objects -- never a concrete renderer), ``redaction`` (functions only),
``locking``, ``gitcontext``, ``protocol`` (``build_protocol`` /
``set_active_protocol`` / ``without_marker_lines``), ``launch``, ``errors``, and a
resolved ``Backend``
(``cli`` resolves it through the registry and passes it in). It must not know which
concrete Backend it holds, nor how that Backend consumes the argv or produces its
events, nor how the Run console words or paints anything it is handed.

See also: ``console`` (the Run console and its rendering apparatus), ``launch``
(owns the wrapped argv, the profile gate, and recovery-command formatting), ``cli``
(``run`` resolves the Backend, acquires the lock, constructs the console, then
calls ``run_locked``; ``clean`` borrows ``retained_runs`` to count what it is about
to destroy), ``backends`` (the registry and Protocol).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import stat
from typing import Any
import uuid

from .backends import Backend
from .console import (
    HOST_ISOLATION_PROPERTY,
    IN_SCOPE_BACKEND_DEVIATIONS,
    NO_SANDBOX_DEVIATION,
    PREFLIGHT_PROPERTIES,
    Deviation,
    IterationOutcome,
    OperatorHelp,
    RunConsole,
    RunSettings,
    RunSummary,
)
from .errors import (
    HandoffError,
    RalphError,
    StartedIterationError,
)
from .gitcontext import command, interactive_only_issues, write_json
from .launch import (
    CaffeinateAssertion,
    Invocation,
    additional_in_scope,
    establish_sandbox,
    prepare_in_scope_state,
    resume_command,
    restart_command,
)
from .locking import secure_state_directory
from .protocol import build_protocol, set_active_protocol, without_marker_lines
from .redaction import collect_secrets, set_active_redactor


# Where a run's evidence is retained beneath the Ralph state root. Named once here,
# in the module that creates those directories, so ``clean`` can count what it is
# about to destroy without inventing a second copy of the layout.
RUNS_DIRECTORY = "runs"


def retained_runs(state_root: Path) -> int:
    """How many runs' evidence *state_root* holds, for the report ``clean`` prints
    before it removes the tree (register G22). A state root that has never held a run
    counts zero rather than failing.

    A ``runs`` entry that is a symlink or not a directory counts zero and is never
    descended into. ``lstat`` decides that, because ``scandir`` would happily walk a
    symlink: a run refuses a symlinked ``runs`` outright, and ``rmtree`` removes such a
    link without touching what it points at, so counting through one would report
    evidence destroyed that is still on disk — and would make the report a second way
    to read outside the state root."""
    runs_root = state_root / RUNS_DIRECTORY
    try:
        info = os.lstat(runs_root)
    except FileNotFoundError:
        return 0
    # S_ISDIR is false for the symlink itself, so this rejects both cases at once.
    if not stat.S_ISDIR(info.st_mode):
        return 0
    with os.scandir(runs_root) as entries:
        return sum(1 for entry in entries if entry.is_dir(follow_symlinks=False))


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


def close_iteration(
    *,
    console: RunConsole,
    announced: int,
    iterations: int,
    began_at: datetime,
    outcome: str,
    session_id: str | None,
) -> None:
    """Close a stopped Iteration with the outcome block every terminal path owes it
    (register G9): its number, the budget it belongs to, how long it ran, what
    happened to it, and the session to resume when one exists. The raising paths reach
    this; the normal return builds its own block because only that path has a
    concluding message to show.

    *announced* is the Iteration the operator was told had started and that has not
    closed yet, or zero when none is open -- before the first rule, and between one
    Iteration's own block and the next Iteration's rule. Zero emits nothing, so a
    block can neither announce an Iteration the operator never saw start nor close one
    that already closed."""
    if not announced:
        return
    console.iteration_finished(
        IterationOutcome(
            number=announced,
            iterations=iterations,
            duration_seconds=(datetime.now(timezone.utc) - began_at).total_seconds(),
            outcome=outcome,
            session_id=session_id,
            concluding_message=None,
        )
    )


def handoff_help(
    *,
    error: HandoffError | StartedIterationError,
    invocation: Invocation,
    remaining: int,
    run_id: str,
) -> OperatorHelp:
    """Build the full help block for a consuming stop from the run's facts and the
    recovery commands the Launch chain produces (register G14). A ``HandoffError``
    carries a session to resume and an operator-facing detail; a ``StartedIterationError``
    consumed its slot with nothing to resume, so it omits both. The console words the
    ``RALPH NEEDS OPERATOR`` block; this function never formats operator text.

    Both commands come from the run's whole ``Invocation``, so a flag the operator
    declared cannot go missing from one command while reaching the other -- which is
    how ``--in-scope-backend`` came to be dropped from budget exhaustion alone. The
    continuation is offered what the handoff left of the budget."""
    session_id = getattr(error, "session_id", None)
    detail = getattr(error, "detail", None)
    return OperatorHelp(
        reason=error.reason,
        run_id=run_id,
        remaining=remaining,
        backend=invocation.backend,
        session_id=session_id,
        detail=detail,
        resume_command=(
            resume_command(invocation, session_id) if session_id else None
        ),
        continue_command=(
            restart_command(invocation, remaining) if remaining else None
        ),
    )


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
    # The run's whole resolved invocation, read once here and handed to every
    # recovery command the run can print. One reading means a handoff and budget
    # exhaustion can never disagree about which flags the run had (findings 4 and
    # 12 of #36's review were exactly that disagreement).
    invocation = Invocation.of(args, worktree, prompt_path)
    in_scope = invocation.in_scope
    # Redact subscription credentials from every readable and retained stream in
    # case backend output echoes an environment value. This precedes the header so
    # the Run console's choke point already has a live redactor to scrub through.
    set_active_redactor(collect_secrets())

    runs_root = secure_state_directory(git_dir, "ralph", RUNS_DIRECTORY)
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
    started = datetime.now(timezone.utc).isoformat()
    iterations: list[dict[str, Any]] = []
    session_id: str | None = None
    # Whether any Iteration completed. The run-level word is the loop's own
    # decision, taken below from this flag once the range is exhausted, and never
    # inherited from the last Iteration's return: an Iteration that ends without a
    # completion marker is a normal end of iteration and has not run out of budget
    # (register J3). There is deliberately no run-level default sitting here for an
    # Iteration to overwrite -- that was how the run's word came to be the last
    # Iteration's.
    completed = False
    # The slot in flight, kept current for the handoff handlers so a started
    # iteration is charged against its own number.
    number = 0
    # A default the handoff handlers read for the started_at of an iteration that
    # stopped; only the branches reachable after a session started ever consult it.
    iteration_started = started
    # The Iteration that has been announced and not yet closed -- zero when none is
    # open -- and when it began. A terminal path closes exactly the Iteration the
    # operator was told had started, and closes it exactly once: the normal return
    # clears this after emitting its own block, so a failure *between* Iterations (a
    # lost power assertion) cannot re-close one that already closed, and a failure
    # before the first rule closes none. ``iteration_began_at`` is the duration every
    # block is measured from, so one block means one thing however the Iteration
    # ended; its initial value is only ever read when ``announced`` is zero, which
    # emits nothing.
    announced = 0
    iteration_began_at = datetime.now(timezone.utc)
    try:
        # Establish host isolation once per run (the profile is stable across
        # iterations): generate the per-run profile and prove it actually bites via
        # the one-shot self-test before any budget is spent, or stop fail-closed here
        # (register D2/D6/D8) — exactly as the caffeinate startup assertion gates the
        # whole loop. Both backends are wrapped uniformly (#20 OpenCode, #22 Claude);
        # `--unsafe-no-sandbox` relaxes only this, returning no profile so the shared
        # Launch chain runs the backend unconfined (register D7). Inside the try so a
        # self-test failure — a failure once the run directory exists — gets the same
        # summary and full help block as any other (register G10).
        # A declared in-scope backend gets its state pinned into the run directory
        # before the profile is generated, because the profile's write allow-list is
        # derived from the environment the session will run under. Skipped entirely
        # under the sandbox opt-out: with no boundary to fit there is nothing to pin,
        # and relocating a backend's store would be a behavior change the opt-out
        # never asked for (D3/D4 amendment).
        if not args.unsafe_no_sandbox:
            env.update(prepare_in_scope_state(run_dir, args.backend, in_scope))
        sandbox_profile = establish_sandbox(
            args.backend,
            run_dir,
            worktree,
            git_dir / "ralph",
            env,
            no_sandbox=args.unsafe_no_sandbox,
            in_scope=in_scope,
        )
        for name in additional_in_scope(args.backend, in_scope):
            # A second subscription credential readable by the whole process tree is
            # a relaxed guarantee, stated as loudly as the sandbox opt-out is
            # (register G7/G13) so it can never be a silent widening.
            console.deviation(Deviation(IN_SCOPE_BACKEND_DEVIATIONS[name]))
        if args.unsafe_no_sandbox:
            # The gate is silent now; the run states the relaxed host-isolation
            # guarantee loudly here, beside the other deviation warnings (register
            # G7/G13), and the console owns its wording (register G14).
            console.deviation(Deviation(NO_SANDBOX_DEVIATION))
        for number in range(1, args.iterations + 1):
            # The loop-wide sleep assertion must still be held before each fresh
            # session; a lost assertion stops the loop with retained evidence.
            assertion.ensure_alive(number)
            iteration_dir = (
                run_dir
                if number == 1
                else secure_state_directory(run_dir, f"iteration-{number:03d}")
            )
            # Open the Iteration with a rule naming its number and the budget; the
            # Run console words it, so multi-iteration output stays attributable to a
            # specific fresh session (register G2/G9).
            console.iteration_started(number, args.iterations)
            # Announced: from here every exit — the normal return and each of the
            # raising paths — closes this Iteration with an outcome block measured
            # from this instant. The session id is dropped with it: it belongs to the
            # Iteration that has just ended, and a block for *this* one must say "no
            # session id" rather than name its predecessor's session.
            announced, iteration_began_at = number, datetime.now(timezone.utc)
            session_id = None
            agent_deviation = backend.preflight(
                worktree, slug, args.model, env, args.unsafe_allow_agents
            )
            if agent_deviation is not None:
                # Preflight admitted an agent vector under --unsafe-allow-agents and
                # handed the fact back; the run states the relaxed subagent-isolation
                # guarantee loudly through the console (register G7/G14).
                console.deviation(agent_deviation)
            if number == 1:
                # The full Trust boundary is proven once this first preflight has
                # cleared: subscription-only authentication and customization
                # isolation here, host isolation already proven by the sandbox
                # self-test before the loop (omitted, and so not claimed, under
                # --unsafe-no-sandbox). It prints where its proof completes, after the
                # first Iteration's banner, rather than in the header (register G8).
                properties = list(PREFLIGHT_PROPERTIES)
                if not args.unsafe_no_sandbox:
                    properties.append(HOST_ISOLATION_PROPERTY)
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
            iteration_started = datetime.now(timezone.utc).isoformat()
            # A started session consumes its slot whatever the outcome; the loop
            # never restarts a slot itself. The injected console doubles as the
            # narrow Observation sink (register G15): the Backend reports live
            # progress through it while the loop is blocked in this call, and the
            # loop still drives only the Protocol and cannot tell the two apart.
            # The word that comes back names what happened to this Iteration, not to
            # the run; the run-level word is decided below (register J3).
            iteration_outcome, session_id, concluding_message = backend.execute_iteration(
                worktree,
                iteration_dir,
                prompt,
                args.model,
                env,
                args.timeout,
                sandbox_profile,
                observe=console,
            )
            iterations.append(
                {
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "number": number,
                    "outcome": iteration_outcome,
                    "session_id": session_id,
                    "started_at": iteration_started,
                }
            )
            # Close the Iteration with its outcome block: duration, outcome, session
            # id, and the Backend's concluding message truncated for display. The raw
            # message is a fact the console words; nothing is written to disk from the
            # truncation, so the retained artifacts stay byte-identical (register G18).
            # Only this path carries a concluding message: the raising paths below
            # never received one to show.
            console.iteration_finished(
                IterationOutcome(
                    number=number,
                    iterations=args.iterations,
                    duration_seconds=(
                        datetime.now(timezone.utc) - iteration_began_at
                    ).total_seconds(),
                    outcome=iteration_outcome,
                    session_id=session_id,
                    concluding_message=without_marker_lines(concluding_message or ""),
                )
            )
            # Closed: a stop before the next Iteration's rule closes nothing.
            announced = 0
            if iteration_outcome == "complete":
                completed = True
                break
        # The run-level word, decided here by the loop rather than borrowed from the
        # last Iteration: the budget is exhausted exactly when the range ran out with
        # no Iteration having completed (register J3). ``iteration budget exhausted``
        # therefore still greps once, at the summary (register J5).
        outcome = "complete" if completed else "budget_exhausted"
    except (HandoffError, StartedIterationError) as error:
        # A consuming stop: a ``HandoffError`` carries a session to resume; a
        # ``StartedIterationError`` consumed its slot before any session metadata
        # arrived and has none. Both close the same way — record the slot, write the
        # outcome, then summary-before-block so the ``RALPH NEEDS OPERATOR`` banner and
        # its recovery commands stay the last, most visible lines and the summary rings
        # the bell once (register G9/G12). ``handoff_help`` reads the session (absent on
        # the started-iteration error) from the error, so the two need no separate arms.
        session_id = getattr(error, "session_id", None)
        iterations.append(
            {
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "number": number,
                "outcome": error.outcome,
                "reason": error.reason,
                "session_id": session_id,
                "started_at": iteration_started,
            }
        )
        outcome = error.outcome
        # Close the Iteration that stopped the loop with its own outcome block. The
        # Iteration an operator most wants attributed is the one that timed out,
        # handed off, or violated the contract, and it used to jump straight from its
        # rule to the run summary with no block, no duration and no session id at all
        # (register G9/J6). It prints before the summary, so the summary and the
        # ``RALPH NEEDS OPERATOR`` banner keep the last lines. Nothing came back from
        # the raising path, so the block carries no concluding message.
        close_iteration(
            console=console,
            announced=announced,
            iterations=args.iterations,
            began_at=iteration_began_at,
            outcome=error.outcome,
            session_id=session_id,
        )
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
        console.run_finished(build_summary(outcome, run_dir, branch, final_branch, status))
        console.operator_help(
            handoff_help(
                error=error,
                invocation=invocation,
                remaining=args.iterations - number,
                run_id=run_dir.name,
            )
        )
        return 2
    except RalphError as error:
        # A failure inside a started Iteration closes it too, for the same reason the
        # consuming stops above do. A failure before the first Iteration's rule -- the
        # profile gate, the in-scope state -- belongs to no Iteration and
        # ``close_iteration`` prints nothing for it.
        close_iteration(
            console=console,
            announced=announced,
            iterations=args.iterations,
            began_at=iteration_began_at,
            outcome="backend_failure",
            session_id=session_id,
        )
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
        # A backend failure that left a run directory behind gets the full help block
        # once evidence exists (register G10): the summary names the git outcome and
        # the evidence path, then failure_help states what failed and points at the
        # run directory. It is not a resumable handoff — a pre-session failure has no
        # session and never shows the RALPH NEEDS OPERATOR banner — so the loop words
        # and returns it here rather than re-raising to the one-line handler.
        console.run_finished(
            build_summary("backend_failure", run_dir, branch, final_branch, status)
        )
        console.failure_help(str(error), run_dir)
        return 2
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
    if outcome == "budget_exhausted":
        # The operator has just spent the whole budget without completion; the next
        # thing they do is run it again, so state the exact continuation command
        # (register G10). The Launch chain builds it from the run's own facts with the
        # full budget restored, so it is a fresh run rather than a zero-iteration one.
        console.budget_continue(restart_command(invocation, invocation.iterations))
    return 0 if outcome == "complete" else 1
