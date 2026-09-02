"""The Ralph command-line entry point: argument parsing and the run/clean/resume
commands.

Invariants:
- ``run`` validates the iteration budget (1..100), the timeout (finite, zero or
  positive, at most ``MAX_ITERATION_TIMEOUT_SECONDS``), the interactive-only label
  (non-empty after stripping, and free of the line breaks that would let it
  fabricate a bullet of its own where it interpolates into the Loop protocol — a
  GitHub label can hold neither), and the model before any budget is spent,
  resolves the default model per backend, then acquires the worktree lock and
  hands off to the Loop. The Loop resolves the concrete interactive-only children
  via ``gh`` and publishes the Loop protocol once the trust boundary is proven,
  not ``run``: the resolution rides the ``gh`` dependency preflight proves and so
  cannot precede it.
  ``resume`` takes no label: recovery is already the interactive session the label
  exists to demand.
- ``clean`` removes only a real ``.git/ralph`` state directory, never following a
  symlink or deleting an unexpected file type, and refuses while a live loop holds
  the worktree lock. It then reports what it destroyed through the console, counting
  the runs while they still exist and distinguishing a real removal from a no-op, so
  a command that irreversibly deletes every run's evidence is never silent about
  having done it (register G22). It arms the redactor before it renders anything, as
  ``run`` and ``resume`` do: the console's choke point only scrubs what a live
  redactor knows about, so a command that prints a resolved path has to arm one
  (register G17). The refusals reach the operator the same way every other failure
  does, as a ``RalphError`` the console words.
- ``resume`` re-establishes the full Trust boundary (sanitized environment,
  per-session OAuth/routing proof, isolated configuration, full-auto permissions,
  caffeinate, and host isolation) before ``exec``-ing the interactive backend, so
  recovery can never inherit unsafe ambient routing and is confined identically to
  automated iterations (register D9). It resolves the Backend through the registry,
  establishes the sandbox through ``launch.establish_sandbox`` (skipped only under
  ``--unsafe-no-sandbox``), and obtains its wrapped argv from ``launch.session_argv``,
  the one seam #19 edits. Recovery relaxes the same guarantees a run does, so it
  states them as loudly: an admitted agent vector (the ``Deviation`` ``preflight``
  hands back) and the sandbox opt-out are stated through the injected console
  (register G7/G13), which is why ``main`` passes it in. The dangerous full-auto
  caveat goes through it too (``relaunching_full_auto``), so this module holds no
  terminal write of its own and the structural rule that only the Run console
  addresses a terminal now holds without exception (register G13).
- ``resume`` states its compact header — the session being entered, the trust
  boundary re-proven, and the host-isolation status — after the sandbox is
  established and immediately before it hands over, because the next statement
  replaces this process and there is nothing left to render afterwards (register
  G8/G22). It is where the proof completes, the same placement register G8 gives a
  run's Trust boundary line, and host isolation is stated whether or not it holds:
  an operator reading three lines cannot tell an omitted guarantee from a kept one.
- ``main`` is the single place a ``RalphError`` becomes ``ralph: <message>`` on
  stderr with exit code 2; the console script and ``python -m ralph.cli`` both run
  it, and the name ``main`` is preserved for the packaging entry point.
- ``main`` is the composition root (register G16): it is the only module in the
  tree that constructs a concrete Run console, and it injects it into all three
  commands and uses it for the terminal error line — ``clean`` and ``resume`` too,
  since a command that destroys evidence or hands over a session addresses the
  operator exactly as a run does. Everything below depends on the ``RunConsole``
  abstraction only, so both rendering choices an operator makes are made here and
  nowhere else. ``--verbose`` hands the console a second stream — stdout — for the
  Backend feed, which is otherwise suppressed so the default view is the dashboard
  alone (register G2); ``--quiet`` drops the status line and the
  Iteration blocks while the header, the summary, and every failure still print
  (register G11). Both are ``run`` flags, defaulted off on the shared parser so
  ``clean`` and ``resume`` carry them too. Whether the console paints for a terminal
  or plain is nobody's choice: it is read from the stream at emit time.

Depends on / must not know: the ``backends`` package (defaults, the registry, and
the resolved Backend's five interface names), ``console`` (the ``RunConsole``
abstraction and the one concrete renderer it selects), ``redaction`` (functions
only), ``protocol`` (the default interactive-only label),
``gitcontext``, ``launch`` (``session_argv``, ``establish_sandbox``), ``locking``
(the worktree lock and ``secure_state_directory``), ``loop`` (``run_locked``, and
``retained_runs`` so ``clean`` never has to know how runs are laid out inside the
state root), ``process`` (timeout ceiling), and ``errors``. It resolves the Backend
once and drives it only through the interface; it must not contain any Backend,
Launch chain, or Loop mechanism of its own, nor branch on the backend name. It words
no operator-facing line of its own at all: every one goes through the injected Run
console.

See also: ``console`` (the Run console it constructs), ``loop`` (the budgeted
Iteration loop), ``backends`` (the registry and adapters), ``launch`` (wrapped argv
and recovery-command formatting), package docstring in ``ralph`` (the map).
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
from .console import (
    IN_SCOPE_BACKEND_DEVIATIONS,
    NO_SANDBOX_DEVIATION,
    PREFLIGHT_PROPERTIES,
    CleanOutcome,
    Deviation,
    ResumeSettings,
    RunConsole,
    StreamRunConsole,
)
from .errors import RalphError
from .gitcontext import command, git_context, read_prompt
from .launch import (
    additional_in_scope,
    establish_sandbox,
    prepare_in_scope_state,
    session_argv,
)
from .locking import WorktreeLock, secure_state_directory
from .loop import retained_runs, run_locked
from .process import MAX_ITERATION_TIMEOUT_SECONDS
from .protocol import DEFAULT_INTERACTIVE_LABEL
from .redaction import collect_secrets, set_active_redactor


def run(args: argparse.Namespace, console: RunConsole) -> int:
    if not 1 <= args.iterations <= 100:
        raise RalphError("iterations must be between 1 and 100")
    if not math.isfinite(args.timeout) or args.timeout < 0:
        raise RalphError("timeout must be zero or positive and finite")
    if args.timeout > MAX_ITERATION_TIMEOUT_SECONDS:
        raise RalphError(
            f"timeout must not exceed {MAX_ITERATION_TIMEOUT_SECONDS} seconds so backend "
            "request and Bash limits stay subordinate to Ralph's timer"
        )
    interactive_label = args.interactive_label.strip()
    if not interactive_label:
        raise RalphError("--interactive-label must not be empty or whitespace")
    if "\n" in interactive_label or "\r" in interactive_label:
        # The label interpolates raw into the Loop protocol's Markdown, so a line
        # break would let a typo fabricate a protocol bullet of its own. A GitHub
        # label can hold neither character, so nothing legitimate is refused.
        raise RalphError(
            "--interactive-label must not contain a newline or carriage return"
        )
    # Carry the stripped label as the value the Loop resolves and publishes; the
    # concrete blocked children can only be resolved once preflight has proven the
    # shared gh dependency, so the Loop builds and publishes the protocol, not run.
    args.interactive_label = interactive_label
    backend = resolve(args.backend)
    args.model = args.model or DEFAULT_MODELS[args.backend]
    backend.validate_model(args.model)

    prompt_path, prompt = read_prompt(args.prompt)
    worktree, git_dir, branch, status, slug = git_context(args.worktree)
    with WorktreeLock(git_dir, git_dir / "ralph" / "lock.json"):
        return run_locked(
            backend,
            args,
            prompt_path,
            prompt,
            worktree,
            git_dir,
            branch,
            status,
            slug,
            console,
        )


def clean(args: argparse.Namespace, console: RunConsole) -> int:
    # Establish the redactor before anything is rendered, exactly as ``run`` and
    # ``resume`` do. The console's choke point is only as good as the live redactor
    # behind it, so a command that prints a resolved path has to arm it too
    # (register G17).
    set_active_redactor(collect_secrets())
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
            outcome = CleanOutcome(state_root, runs=None)
        else:
            # Never follow a symlink or delete an unexpected file type: only a real
            # Ralph state directory is removed, and shutil.rmtree does not follow
            # symlinked children, so backend transcripts and source files outside
            # .git/ralph are never touched.
            if stat.S_ISLNK(info.st_mode):
                raise RalphError("refusing to remove a symlinked Ralph state path")
            if not stat.S_ISDIR(info.st_mode):
                raise RalphError("Ralph state path is not a directory")
            # Count what is about to be destroyed while it still exists; the report
            # names the evidence that went, not the empty space it left.
            outcome = CleanOutcome(state_root, runs=retained_runs(state_root))
            shutil.rmtree(state_root)
    finally:
        lock.release()
    # Reported after the lock is released and only once the removal actually
    # succeeded, so a failed rmtree raises instead of claiming a delete that did
    # not happen.
    console.state_removed(outcome)
    return 0


def resume(args: argparse.Namespace, console: RunConsole) -> int:
    backend = resolve(args.backend)
    backend.validate_model(args.model)
    worktree, git_dir, _branch, _status, slug = git_context(args.worktree)
    # Re-establish the exact sanitized child environment and re-prove the
    # subscription trust boundary (OAuth, effective routing, model availability,
    # customization isolation) before any resumed model work. reject_unsafe_-
    # environment inside preflight fails closed on a newly added API credential
    # or custom endpoint, so recovery cannot silently inherit unsafe routing.
    env = backend.environment(args.model)
    set_active_redactor(collect_secrets())
    agent_deviation = backend.preflight(worktree, slug, args.model, env, args.unsafe_allow_agents)
    if agent_deviation is not None:
        # Recovery relaxes the same guarantees the run did; an admitted agent vector
        # is stated as loudly on resume as it is on a run (register G7/G14).
        console.deviation(agent_deviation)
    # Re-establish host isolation identically to automated iterations (register
    # D9): the same generator and one-shot self-test, writing the concrete profile
    # under the untracked .git/ralph (register D10), then wrapping it around the
    # interactive argv via session_argv. --unsafe-no-sandbox relaxes only this,
    # exactly as in `run`, so a session run unconfined resumes unconfined too.
    in_scope = tuple(args.in_scope_backend or ())
    resume_dir = secure_state_directory(git_dir, "ralph", "resume")
    # Recovery re-establishes the identical boundary the run had, declared lanes
    # included (register D9): the same state seeding, the same generator, the same
    # self-test. Without this a handed-off session would be re-confined out of the
    # very backend the run was using when it stopped.
    if not args.unsafe_no_sandbox:
        env.update(prepare_in_scope_state(resume_dir, args.backend, in_scope))
    sandbox_profile = establish_sandbox(
        args.backend,
        resume_dir,
        worktree,
        git_dir / "ralph",
        env,
        no_sandbox=args.unsafe_no_sandbox,
        in_scope=in_scope,
    )
    for name in additional_in_scope(args.backend, in_scope):
        console.deviation(Deviation(IN_SCOPE_BACKEND_DEVIATIONS[name]))
    if args.unsafe_no_sandbox:
        # The gate is silent; recovery states the relaxed host-isolation guarantee
        # loudly through the console, exactly as `run` does (register G7/G13).
        console.deviation(Deviation(NO_SANDBOX_DEVIATION))
    # The Launch chain assembles the wrapped argv: caffeinate outermost, the
    # sandbox-exec host-isolation wrap inside it, both launched by absolute path
    # exactly as automated iterations do (preflight has proved caffeinate exists).
    # Holding the -im assertion for the interactive session's whole lifetime
    # replaces Ralph's own loop-level assertion once control passes to the operator.
    argv = session_argv(
        backend.resume_argv(worktree, args.model, args.session), sandbox_profile
    )
    # The compact recovery header, printed where its proof completes and while there
    # is still a process to print it from: the next statement replaces this one, so
    # everything the operator is going to be told about the handover has to be said
    # now (register G8/G22). The properties named are the ones ``backend.preflight``
    # has just re-established; ``cli`` names which, the console words them (G14).
    console.resume_started(
        ResumeSettings(
            backend=args.backend,
            model=args.model,
            session_id=args.session,
            host_isolated=not args.unsafe_no_sandbox,
            reproven=PREFLIGHT_PROPERTIES,
        )
    )
    console.relaunching_full_auto()
    try:
        os.chdir(worktree)
        os.execvpe(argv[0], argv, env)
    except OSError as error:
        raise RalphError(f"could not launch {args.backend} for resume: {error.strerror}") from None
    return 2


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="ralph")
    # The two rendering flags belong to ``run``, but every command constructs the
    # console, so they are defaulted here rather than read back with a fallback.
    result.set_defaults(verbose=False, quiet=False)
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
        "--interactive-label",
        default=DEFAULT_INTERACTIVE_LABEL,
        metavar="LABEL",
        help=(
            "issue label marking a child as reserved for an interactive operator "
            "session; the Loop protocol tells the backend to treat such children as "
            "blocked for autonomous iterations and to halt for input when only "
            f"labelled work remains (default: {DEFAULT_INTERACTIVE_LABEL}). Selection "
            "stays advisory -- Ralph cannot observe which child the backend picks -- "
            "so only the needs-input halt is enforced"
        ),
    )
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
    run_parser.add_argument(
        "--unsafe-no-sandbox",
        action="store_true",
        help=(
            "disable host isolation: skip the Seatbelt sandbox wrap and its "
            "self-test so the backend runs unconfined and may write outside the "
            "worktree or read the operator's credentials (unsafe). Separate from "
            "and orthogonal to --unsafe-allow-agents; relaxes only host isolation"
        ),
    )
    run_parser.add_argument(
        "--in-scope-backend",
        action="append",
        choices=["claude", "opencode"],
        default=None,
        metavar="BACKEND",
        dest="in_scope_backend",
        help=(
            "declare that this run will also dispatch work to BACKEND, making that "
            "backend's subscription credential readable (and, for OpenCode, "
            "writable so a token refresh persists). Repeatable. Ralph otherwise "
            "denies every backend it is not running, which assumes one backend per "
            "run and blocks a run whose work uses both model families. sandbox-exec "
            "confines the whole process tree, so a declared credential is readable "
            "by every command in the run, not only by that backend"
        ),
    )
    run_parser.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "restore the backend's running commentary on stdout, each line prefixed "
            "with the backend or the specific subagent that produced it; the "
            "dashboard stays on stderr, so redirecting one leaves the other on the "
            "terminal"
        ),
    )
    run_parser.add_argument(
        "--quiet",
        action="store_true",
        help=(
            "suppress the status line and the per-iteration blocks for an unattended "
            "run; the run header, the run summary, warnings, and every failure block "
            "still print"
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
    resume_parser.add_argument(
        "--in-scope-backend",
        action="append",
        choices=["claude", "opencode"],
        default=None,
        metavar="BACKEND",
        dest="in_scope_backend",
        help=(
            "declare that this run will also dispatch work to BACKEND, making that "
            "backend's subscription credential readable (and, for OpenCode, "
            "writable so a token refresh persists). Repeatable. Ralph otherwise "
            "denies every backend it is not running, which assumes one backend per "
            "run and blocks a run whose work uses both model families. sandbox-exec "
            "confines the whole process tree, so a declared credential is readable "
            "by every command in the run, not only by that backend"
        ),
    )
    resume_parser.add_argument(
        "--unsafe-no-sandbox",
        action="store_true",
        help=(
            "disable host isolation: skip the Seatbelt sandbox wrap and its "
            "self-test so the resumed backend runs unconfined and may write "
            "outside the worktree or read the operator's credentials (unsafe). Separate "
            "from and orthogonal to --unsafe-allow-agents; relaxes only host "
            "isolation"
        ),
    )
    return result


def main() -> int:
    args = parser().parse_args()
    # The composition root: the one place a concrete Run console is selected and
    # injected (register G16). Every module below here depends on the abstraction.
    # The four concrete renderings -- terminal, plain, quiet, verbose -- are selected
    # here and only here: the dashboard on stderr, the opt-in Backend feed on stdout
    # (register G11). ``clean`` and ``resume`` carry neither flag, so both default off.
    console = StreamRunConsole(
        sys.stderr, feed=sys.stdout if args.verbose else None, quiet=args.quiet
    )
    try:
        if args.command == "run":
            return run(args, console)
        if args.command == "clean":
            return clean(args, console)
        if args.command == "resume":
            return resume(args, console)
    except RalphError as error:
        console.failed(str(error))
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
