"""The Run console: the one module permitted to address the operator, and the
rendering apparatus — palette, terminal detection, dynamic width, no-wrap
truncation, and the redaction choke point — that sits behind its small interface.

Invariants:
- ``RunConsole`` is the abstraction the rest of Ralph depends on; ``StreamRunConsole``
  is the concrete renderer and ``cli`` is the only module that constructs one
  (register G16). The Loop and the Backend adapters hand over value objects and
  plain facts and never construct operator-facing text (register G14).
- Every operator-facing string this module emits passes through ``redact`` in
  ``_write``, the single choke point for console output (register G17). Retained
  artifacts keep their own redaction; console truncation is display-only and never
  reduces what is written to disk (register G18).
- The palette has four roles (register G12). ``chrome`` marks Ralph's own voice,
  ``warning`` and ``failure`` escalate; ``content`` is deliberately uncoloured so
  Backend text can never be recoloured into Ralph's voice. Colour is emitted only
  when the stream is a terminal and ``NO_COLOR`` is unset or empty, so a piped or
  redirected log carries no ANSI at all.
- Terminal-ness and width are read at emit time, never cached, so a resized window
  is honoured mid-run. A non-terminal stream has no width and is never truncated;
  a terminal header fact is fitted to the measured width by shortening its value —
  head-first for prose, tail-first for paths, where the run id and the file name
  are the informative end — so it degrades by dropping characters and never by
  folding onto a second row.
- Only header facts are fitted. A warning or a failure is emitted whole, wrapping
  if it must: its wording is what an operator greps for and what register G19 pins
  byte-identical, so clipping words out of one would break the contract the header
  has no part in. The header can afford the trade because every fact it shortens
  is recoverable from the run directory it names.
- ``run_started`` states the resolved settings and the evidence path (register G8).
  The standing full-auto and power-assertion caveats are stated there as settings,
  not shouted as ``WARNING:`` paragraphs, so the rare real warnings stand out
  (register G7). The concrete interactive-only children cannot be resolved before
  the first Iteration's preflight proves ``gh``, so they complete the header from
  ``interactive_only_resolved`` where their resolution finishes — the same
  treatment register G8 gives the Trust boundary line.
- Each Iteration opens with a rule (``iteration_started``) and closes with an outcome
  block (``iteration_finished``) carrying its duration, outcome, session id, and the
  Backend's concluding message truncated for display; every run ends with a summary
  (``run_finished``) naming the git outcome and the evidence path, on every terminal
  path including success (register G9). The bell rings in ``run_finished`` on every
  terminal outcome but only on a terminal, so a piped log carries no bell character
  (register G12). The concluding message is truncated for display only — nothing is
  written to disk from it, so the retained copy stays byte-identical (register G18).
  The Trust boundary line (``trust_boundary_established``) is emitted where its proof
  completes, after the first Iteration's preflight, never in the header (register G8).
- The full help block applies to every failure once a run directory exists (register
  G10): a resumable handoff and a consuming stop are worded by ``operator_help`` as the
  byte-identical ``RALPH NEEDS OPERATOR`` banner; a backend failure that left evidence
  behind is worded by ``failure_help`` — the reason plus a next step pointing at the
  run directory — never the banner, because a pre-session failure is not a resumable
  handoff. Argument and precondition failures never reach either and stay the one-line
  ``failed``. Budget exhaustion gains the exact continuation command through
  ``budget_continue``. A relaxed guarantee — ``--unsafe-no-sandbox`` or an admitted
  agent vector — is stated loudly through ``deviation`` (register G7). The handoff
  block, the deviations, and the continuation line carry their own first-column
  vocabulary and so are emitted verbatim through ``_line``, without the header's
  ``ralph: `` prefix, but through the same redaction choke point (register G17). The
  Launch chain, not the console, produces the recovery commands ``operator_help`` and
  ``budget_continue`` render (register G14).
- ``observe`` is the narrow one-method Observation sink the Backend adapters drive
  during an Iteration (register G14/G15). It carries a closed set of frozen value
  types -- progress facts (``ToolObserved``, ``ContextObserved``, ``SubagentsObserved``)
  and the mid-run warnings an adapter used to print itself (``MarkerWithdrawn``,
  ``UnmarkedQuestion``, ``KilledTask``); the adapter emits facts and the console
  decides wording, so the load-bearing ``withdrew it``, ``unmarked operator-directed``,
  and the killed-task phrase live here now, not in the adapter (register G19). The
  progress facts feed a single status line that repaints in place on a terminal
  (register G3/G4): Iteration and budget, the Iteration's elapsed time, the current
  or last tool, the tool count, the orchestrator's live context size, and the live
  subagent count. Its ticking elapsed clock is the only motion -- a background ticker
  repaints it on a cadence so a long silent tool call still advances the clock, and a
  frozen clock therefore means stalled rather than a spinner spinning over a hang.
  The line is read against the terminal's live width and never wraps: it drops fields
  right-to-left (``render_status``) and clips rather than folding. It is painted only
  once the Iteration has produced at least one Observation, so an Iteration that emits
  none never paints one. The status line is erased before any other operator line
  prints and redrawn after, so a warning or a header fact never corrupts it. Off a
  terminal there is no in-place line: the ticker degrades to slow append-only
  heartbeat lines carrying the same facts and no ANSI, so a piped log stays clean
  (register G3/G11). The status apparatus uses only ``\\r`` and spaces to repaint, never
  cursor-control escapes, so ``NO_COLOR`` on a terminal still emits no escape at all.

Depends on / must not know: ``redaction`` (functions only, never the active-redactor
global). It must not know how a run is driven, which Backend it holds, or what any
value object it renders was computed from.

See also: ``cli`` (the composition root; constructs the concrete console and passes
it in), ``loop`` (emits the run's facts), CONTEXT.md (**Run console**).
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import threading
import time
from typing import Protocol, TextIO

from .redaction import redact


# The four-role palette (register G12), emitted only on a terminal. ``content`` is
# the terminal's own foreground on purpose: Ralph's chrome is what is coloured, so
# Backend text printed elsewhere cannot counterfeit Ralph's voice by colour alone.
PALETTE = {
    "chrome": "\033[36m",
    "content": "",
    "warning": "\033[33m",
    "failure": "\033[1;31m",
}
RESET = "\033[0m"

# Ralph's voice off a terminal: the prefix that makes its own lines greppable and
# distinguishable from Backend output in a piped log, where colour cannot.
PREFIX = "ralph: "

# Used only when the stream claims to be a terminal but refuses to report a usable
# size; a real ioctl answer is always preferred, and a non-terminal has no width.
FALLBACK_TERMINAL_WIDTH = 80
ELLIPSIS = "..."

# The terminal bell, rung on every terminal outcome (register G12) — on a terminal
# only, so a piped or redirected log never carries the control character.
BELL = "\a"

# The concluding message is truncated for display so a long final narration does not
# fill the outcome block; the retained artifacts keep the whole of it (register G18).
CONCLUDING_MESSAGE_LIMIT = 200

# The run summary's headline per terminal outcome. ``budget_exhausted`` keeps the
# byte-identical ``iteration budget exhausted`` phrase an operator greps for
# (register G19); an unknown outcome degrades to naming itself rather than vanishing.
OUTCOME_HEADLINES = {
    "complete": "run complete",
    "budget_exhausted": "run incomplete: iteration budget exhausted without completion",
    "needs_input": "run handed off for operator input",
    "backend_contract_failure": "run failed: backend contract violation",
    "backend_failure": "run failed: backend error",
    "timeout": "run failed: iteration timed out",
}

# The deviation warnings a run states loudly when a standing guarantee is relaxed
# (register G7): ``--unsafe-no-sandbox`` drops host isolation, and
# ``--unsafe-allow-agents`` drops the proof of the backend's subagent isolation.
# Their wording lives here, the one place operator-facing text is worded (register
# G14) -- the Loop, ``cli``, and the Backend adapters only name which deviation
# occurred. The load-bearing phrases ``--unsafe-no-sandbox is set`` and each
# ``Ralph is not proving ... isolation`` are preserved byte-identical so the events
# an operator greps for survive the redesign (register G19).
NO_SANDBOX_DEVIATION = "no-sandbox"
CLAUDE_AGENTS_DEVIATION = "claude-agents"
OPENCODE_AGENTS_DEVIATION = "opencode-agents"

DEVIATION_TEXTS = {
    NO_SANDBOX_DEVIATION: (
        "WARNING: --unsafe-no-sandbox is set; Ralph is NOT proving host isolation for "
        "this session. The backend runs unconfined and may write outside the worktree "
        "or read the operator's credentials. No other guarantee is relaxed."
    ),
    CLAUDE_AGENTS_DEVIATION: (
        "WARNING: --unsafe-allow-agents is set; Ralph is not proving "
        "Claude subagent isolation for this run."
    ),
    OPENCODE_AGENTS_DEVIATION: (
        "WARNING: --unsafe-allow-agents is set; Ralph is not proving "
        "OpenCode agent isolation for this run."
    ),
}


@dataclass(frozen=True)
class RunSettings:
    """The resolved settings a run is about to spend budget on, plus where its
    evidence will live. A value object: the Loop fills it, the console words it."""

    backend: str
    model: str
    iterations: int
    timeout: float
    repository: str
    branch: str
    worktree: Path
    prompt_path: Path
    interactive_label: str
    run_dir: Path
    dirty: bool


@dataclass(frozen=True)
class IterationOutcome:
    """How one Iteration closed: its number and the budget it belongs to, how long
    the session ran, the outcome the Loop recorded, the session id to resume, and the
    Backend's concluding message. The Loop fills it with facts; the console truncates
    the message and words the block (register G14)."""

    number: int
    iterations: int
    duration_seconds: float
    outcome: str
    session_id: str | None
    concluding_message: str | None


@dataclass(frozen=True)
class RunSummary:
    """The git outcome and evidence path a run ends on, on every terminal path. The
    Loop computes the facts — the final branch, whether the worktree is dirty, and
    whether the branch's commits reached its upstream — and the console words them
    and rings the bell (register G9, G12, G14)."""

    outcome: str
    run_dir: Path
    initial_branch: str
    final_branch: str
    dirty: bool
    upstream: str | None
    ahead: int


@dataclass(frozen=True)
class Deviation:
    """A standing guarantee a run relaxed, named by the site that relaxed it and
    worded loudly by the console (register G7/G14). ``kind`` selects one of
    ``DEVIATION_TEXTS``; the site that raises it -- the Loop for the sandbox opt-out,
    a Backend adapter for an admitted agent vector -- constructs no operator text."""

    kind: str


@dataclass(frozen=True)
class OperatorHelp:
    """The full help block a run prints when it stops and leaves a run directory full
    of evidence behind (register G10): the reason it stopped, the run to point at, the
    session to resume when one exists, the remaining budget, and the recovery commands
    the Launch chain produced. The Loop fills it with facts and pre-built commands; the
    console words the block and keeps its ``RALPH NEEDS OPERATOR`` vocabulary
    byte-identical (register G14/G19)."""

    reason: str
    run_id: str
    remaining: int
    backend: str
    session_id: str | None = None
    detail: str | None = None
    resume_command: str | None = None
    continue_command: str | None = None


# The closed set of Observation value types the narrow sink carries (register
# G14/G15). Each is a frozen fact an adapter emits during an Iteration; the console
# decides what to do with it. A new Observation is a new value type here, not a wider
# interface, so the seam the adapters see stays one method while behaviour grows.
@dataclass(frozen=True)
class ToolObserved:
    """The orchestrator reached for a tool. ``name`` is the tool the status line
    shows as the current or last tool, and each one increments the tool count."""

    name: str


@dataclass(frozen=True)
class ContextObserved:
    """The orchestrator's live prompt size, in absolute tokens (register G5): never
    a percentage, never cumulative across the run, never inclusive of the separate
    subagent windows. The per-Backend arithmetic that produced it lives in the
    adapter; the console only shows the latest gauge."""

    tokens: int


@dataclass(frozen=True)
class SubagentsObserved:
    """The live subagent roster -- the descriptions of the background subagents in
    flight. The status line shows the count, which is what explains a silent
    orchestrator waiting on them; the descriptions leave room for a later view."""

    roster: tuple[str, ...]


@dataclass(frozen=True)
class MarkerWithdrawn:
    """The Backend raised a needs-input marker in a message other than the one the
    Iteration was judged on, then spoke past it: a question it withdrew. ``quote`` is
    the raw fragment; the console redacts, bounds, and words it (register G17/G19)."""

    quote: str


@dataclass(frozen=True)
class UnmarkedQuestion:
    """The Backend's final message ended on an operator-directed question with no
    marker: a low-confidence signal the run warns on and continues past. ``quote`` is
    the raw fragment the console redacts, bounds, and words (register G17/G19)."""

    quote: str


@dataclass(frozen=True)
class KilledTask:
    """The Backend's runtime reported it killed a background task the Backend left
    running when it ended its turn. ``task_id`` is the CLI-generated identifier the
    console names the abandoned task by, or ``None`` when the report carried none."""

    task_id: str | None


Observation = (
    ToolObserved
    | ContextObserved
    | SubagentsObserved
    | MarkerWithdrawn
    | UnmarkedQuestion
    | KilledTask
)


# The status line's fields, in left-to-right priority order (register G4). It drops
# from the right as the window narrows, so the Iteration and elapsed always survive
# and the subagent count is the first to go.
STATUS_SEPARATOR = " · "
# How often the background ticker repaints the in-place status line so its elapsed
# clock keeps ticking through a long silent tool call -- the one motion that
# distinguishes a live run from a hang (register G3).
STATUS_TICK_SECONDS = 1.0
# Off a terminal there is no in-place line to keep smooth, so the ticker degrades to
# *slow* append-only heartbeats at this far coarser cadence (register G3): a live sign
# for a piped log without one line a second flooding it. A run shorter than one
# interval emits no heartbeat at all, which is fine -- a short run needs no liveness.
HEARTBEAT_SECONDS = 30.0


def render_status(
    iteration: int,
    iterations: int,
    elapsed_seconds: float,
    tool: str | None,
    tool_count: int,
    context_tokens: int | None,
    subagents: int,
    width: int | None,
) -> str:
    """Render the status line, dropping fields right-to-left until it fits *width*
    and never wrapping (register G3/G4). A stream with no width (anything that is not
    a terminal) keeps every field. The Iteration and elapsed are never dropped; when
    even they do not fit the line is clipped rather than folded onto a second row."""
    fields = [f"iteration {iteration}/{iterations}", format_duration(elapsed_seconds)]
    if tool:
        fields.append(tool)
    fields.append(f"{tool_count} tool{'' if tool_count == 1 else 's'}")
    if context_tokens is not None:
        fields.append(f"context {context_tokens} tokens")
    fields.append(f"{subagents} subagent{'' if subagents == 1 else 's'}")
    # Drop from the right (subagents first) until the prefixed line fits the window.
    while len(fields) > 2 and width is not None and (
        len(PREFIX) + len(STATUS_SEPARATOR.join(fields)) > width
    ):
        fields.pop()
    text = PREFIX + STATUS_SEPARATOR.join(fields)
    if width is not None and len(text) > width:
        # Even the Iteration and elapsed overflow a very narrow window: clip rather
        # than fold, so the line still occupies exactly one row.
        text = text[:width]
    return text


class RunConsole(Protocol):
    """The operator-facing seam. It widens as later tickets migrate their emit
    sites; it is structural, matched with no runtime class or ABC machinery, in the
    style of the Backend Protocol. ``observe`` is the narrow one-method Observation
    sink (register G15) the wide interface also carries, so the composition root can
    inject the same console into ``execute_iteration`` as the sink; the Backend
    adapters see only that one method, typed as ``backends.ObservationSink``."""

    def observe(self, observation: Observation) -> None: ...

    def run_started(self, settings: RunSettings) -> None: ...

    def interactive_only_resolved(self, label: str, issues: list[int]) -> None: ...

    def deviation(self, warning: Deviation) -> None: ...

    def iteration_started(self, number: int, iterations: int) -> None: ...

    def trust_boundary_established(self, properties: list[str]) -> None: ...

    def iteration_finished(self, outcome: IterationOutcome) -> None: ...

    def run_finished(self, summary: RunSummary) -> None: ...

    def budget_continue(self, command: str) -> None: ...

    def operator_help(self, help: OperatorHelp) -> None: ...

    def failure_help(self, reason: str, run_dir: Path) -> None: ...

    def failed(self, message: str) -> None: ...


def format_timeout(timeout: float) -> str:
    # `--timeout 0` deliberately disables the limit, which is a different fact
    # from "zero seconds" and must not be shown as one.
    return "disabled" if timeout <= 0 else f"{timeout:g}s"


def format_duration(seconds: float) -> str:
    """A compact elapsed duration for an outcome block: whole seconds under a minute,
    then ``m``/``s``, then ``h``/``m``, so a four-hour and a four-second Iteration read
    the same width."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def summarize_message(text: str) -> str:
    """Collapse a Backend concluding message to one line and cap its length for the
    outcome block. Display only: the caller never writes this back to disk, so the
    retained artifacts keep the whole message byte-identical (register G18)."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= CONCLUDING_MESSAGE_LIMIT:
        return collapsed
    return collapsed[: CONCLUDING_MESSAGE_LIMIT - len(ELLIPSIS)] + ELLIPSIS


def fit_line(prefix: str, value: str, width: int | None, *, keep_tail: bool) -> tuple[str, str]:
    """Shorten *value* so ``prefix + value`` occupies at most *width* columns.

    A stream with no width (anything that is not a terminal) is returned
    untouched. ``keep_tail`` preserves the informative end of a path — the run id,
    the file name — where the leading directories are what can be spared."""
    if width is None or len(prefix) + len(value) <= width:
        return prefix, value
    budget = width - len(prefix)
    if budget < len(ELLIPSIS) + 1:
        # Narrower than the prefix can usefully carry: the line degrades to the
        # prefix alone, itself clipped, rather than folding onto a second row.
        return prefix[:width], ""
    if keep_tail:
        return prefix, ELLIPSIS + value[-(budget - len(ELLIPSIS)):]
    return prefix, value[: budget - len(ELLIPSIS)] + ELLIPSIS


class StreamRunConsole:
    """Renders the Run console onto one stream — stderr in production (register
    G11). Only ``cli`` constructs it."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        # All writes -- the main thread's operator lines and the ticker's repaints --
        # go through one re-entrant lock so a repaint never interleaves with a line.
        self._lock = threading.RLock()
        # The status line's live state, reset each Iteration. ``_active`` spans an
        # Iteration; ``_established`` turns on with the first Observation, gating the
        # very first paint so an Iteration that emits none never shows one;
        # ``_painted`` records whether an in-place status currently sits at the cursor.
        self._status_active = False
        self._status_established = False
        self._status_painted = False
        self._painted_width = 0
        self._iteration = 0
        self._iterations = 0
        self._tool: str | None = None
        self._tool_count = 0
        self._context: int | None = None
        self._subagents = 0
        self._iteration_start: float | None = None
        self._last_heartbeat = 0.0
        self._ticker_thread: threading.Thread | None = None
        self._ticker_stop: threading.Event | None = None

    def observe(self, observation: Observation) -> None:
        # The narrow sink the Backend adapters drive. Progress facts update the status
        # line's fields and repaint it; the migrated warnings are worded and emitted
        # as whole operator lines, erasing and redrawing the status around themselves
        # like any other interruption (register G14/G15).
        with self._lock:
            if isinstance(observation, ToolObserved):
                self._tool = observation.name
                self._tool_count += 1
                self._establish_locked()
            elif isinstance(observation, ContextObserved):
                self._context = observation.tokens
                self._establish_locked()
            elif isinstance(observation, SubagentsObserved):
                self._subagents = len(observation.roster)
                self._establish_locked()
            elif isinstance(observation, MarkerWithdrawn):
                self._message(
                    "warning",
                    "warning: the backend requested operator input earlier in the "
                    "session but its final message withdrew it; continuing to the next "
                    f"iteration: {summarize_message(redact(observation.quote))}",
                )
            elif isinstance(observation, UnmarkedQuestion):
                self._message(
                    "warning",
                    "warning: final message ended on an unmarked operator-directed "
                    "question; continuing to the next iteration (no "
                    "<promise>NEEDS_INPUT</promise> marker and no question tool used): "
                    f"{summarize_message(redact(observation.quote))}",
                )
            elif isinstance(observation, KilledTask):
                label = (
                    f"task {observation.task_id}"
                    if observation.task_id
                    else "an unnamed background task"
                )
                self._message(
                    "warning",
                    "warning: the backend killed a background task still running when "
                    "it ended its turn, so its work was left unverified: " + label,
                )

    def run_started(self, settings: RunSettings) -> None:
        self._fact("backend", f"{settings.backend}, model {settings.model}")
        self._fact(
            "iterations",
            f"{settings.iterations}, timeout {format_timeout(settings.timeout)}",
        )
        self._fact("repository", f"{settings.repository}, branch {settings.branch}")
        self._fact("worktree", str(settings.worktree), keep_tail=True)
        self._fact("prompt", str(settings.prompt_path), keep_tail=True)
        self._fact(
            "interactive-only label",
            f"{settings.interactive_label}; children resolved once preflight has proven gh",
        )
        self._fact("run directory", str(settings.run_dir), keep_tail=True)
        # The two standing caveats: stated as settings rather than shouted, so the
        # block where a rare real warning lands is still worth reading (register G7).
        self._fact(
            "permissions",
            "dangerous full-auto; the backend may edit files and run commands "
            "without confirmation",
        )
        self._fact(
            "power assertion",
            "caffeinate -im, which cannot prevent lid-close or explicit sleep, "
            "power loss, or external network and service outages",
        )
        if settings.dirty:
            self._message("warning", "warning: worktree has uncommitted changes")

    def interactive_only_resolved(self, label: str, issues: list[int]) -> None:
        listed = ", ".join(f"#{issue}" for issue in issues) if issues else "none open"
        self._fact("interactive-only children", f"{listed} (label {label})")

    def deviation(self, warning: Deviation) -> None:
        # A relaxed guarantee stays loud (register G7): worded here and painted in
        # the warning role on a terminal. Emitted whole, without the header's
        # ``ralph: `` prefix, because its ``WARNING:`` opener and the phrases behind
        # it are the anchors an operator greps for (register G19) -- so a narrow
        # window may wrap it but never clips a word out of it.
        self._line("warning", DEVIATION_TEXTS[warning.kind])

    def iteration_started(self, number: int, iterations: int) -> None:
        # Open the Iteration's status lifecycle: reset the fields and start the
        # elapsed clock. The status line itself is not painted until the first
        # Observation establishes it, so an Iteration that emits none never shows one.
        self._begin_iteration(number, iterations)
        # A rule opening the Iteration (register G2/G9): a full-width divider on a
        # terminal, embedding the number and the budget; a plain labelled line when
        # the stream has no width, so a piped log stays readable without the fill.
        width = self._width()
        if width is None:
            self._message("chrome", f"iteration {number} of {iterations}")
            return
        opener = f"{PREFIX}── iteration {number} of {iterations} "
        # Fill to the window on a terminal, but never past it: a rule wider than the
        # window is clipped rather than folded onto a second row (register G3).
        line = opener[:width] if width < len(opener) else opener + "─" * (width - len(opener))
        self._paint("", "chrome", redact(line), "chrome")

    def trust_boundary_established(self, properties: list[str]) -> None:
        # Printed where its proof completes — after the first Iteration's preflight —
        # rather than in the header, because the Loop's control flow is deliberately
        # not reordered for cosmetics (register G8). Only the properties actually
        # proven are named, so a relaxed guarantee is never reported as proven.
        self._fact("trust boundary", "proven: " + ", ".join(properties))

    def iteration_finished(self, outcome: IterationOutcome) -> None:
        # The Iteration is over: stop the ticker and erase any painted status line
        # before the outcome block prints, so nothing redraws a stale status below it.
        self._finalize()
        session = outcome.session_id or "no session id"
        self._fact(
            f"iteration {outcome.number} of {outcome.iterations}",
            f"{outcome.outcome} in {format_duration(outcome.duration_seconds)}, "
            f"session {session}",
        )
        # The Backend's concluding message: the one utterance worth keeping, shown as
        # Backend content (uncoloured) and truncated for display only.
        if outcome.concluding_message and outcome.concluding_message.strip():
            self._content(summarize_message(outcome.concluding_message))

    def run_finished(self, summary: RunSummary) -> None:
        # A terminal path may arrive without an ``iteration_finished`` (a failure mid
        # session), so stop the ticker and erase any painted status here too before
        # the summary prints.
        self._finalize()
        # Ring the bell on a terminal only, so an operator who walked away is called
        # back on every terminal outcome and a piped log carries no control character.
        if self._is_terminal():
            with self._lock:
                self._stream.write(BELL)
                self._stream.flush()
        self._fact(
            "outcome", OUTCOME_HEADLINES.get(summary.outcome, f"run ended: {summary.outcome}")
        )
        self._fact("branch", self._branch_phrase(summary))
        self._fact(
            "worktree", "dirty; uncommitted changes remain" if summary.dirty else "clean"
        )
        self._fact("push", self._push_phrase(summary))
        self._fact("evidence", str(summary.run_dir), keep_tail=True)

    @staticmethod
    def _branch_phrase(summary: RunSummary) -> str:
        if summary.final_branch != summary.initial_branch:
            return f"changed from {summary.initial_branch} to {summary.final_branch}"
        return summary.final_branch

    @staticmethod
    def _push_phrase(summary: RunSummary) -> str:
        # Answers "was the run's work pushed?" — keyed on how many commits are ahead
        # of the upstream, not on how far behind it, so a branch that is merely behind
        # is still reported as pushed rather than overclaimed as fully in sync.
        if summary.upstream is None:
            return "no upstream configured; nothing pushed"
        if summary.ahead > 0:
            return f"{summary.ahead} commit(s) not pushed to {summary.upstream}"
        return f"pushed to {summary.upstream}"

    def budget_continue(self, command: str) -> None:
        # Budget exhaustion is not a failure, but the operator has just spent the whole
        # budget and the next thing they do is run it again, so the run states the exact
        # command that continues the work (register G10). The ``continue Ralph:`` anchor
        # is the same one the handoff block uses, so a habit built on one reads the other.
        self._line("chrome", f"continue Ralph: {command}")

    def operator_help(self, help: OperatorHelp) -> None:
        # The full help block once a run directory exists (register G10), byte-identical
        # to the banner an operator greps for. Painted whole in the failure role on a
        # terminal, and without the header's ``ralph: `` prefix: ``RALPH NEEDS
        # OPERATOR``, ``reason:``, ``manual resume:``, and ``continue Ralph:`` are the
        # first-column anchors tooling and habits match on (register G19).
        self._line("failure", "========== RALPH NEEDS OPERATOR ==========")
        self._line("failure", f"reason: {help.reason}")
        self._line("failure", f"ralph run: {help.run_id}")
        if help.session_id:
            self._line("failure", f"{help.backend} session: {help.session_id}")
        if help.detail:
            self._line("failure", f"question/error: {help.detail}")
        if help.session_id and help.resume_command:
            # Without a session there is nothing to resume; the handoff still offers
            # the remaining-budget command so the loop can be continued.
            self._line("failure", f"manual resume: {help.resume_command}")
        self._line("failure", f"iterations remaining: {help.remaining}")
        if help.remaining and help.continue_command:
            self._line("failure", f"continue Ralph: {help.continue_command}")
        else:
            self._line("failure", "No iterations remain to continue Ralph.")
        self._line("failure", "==========================================")

    def failure_help(self, reason: str, run_dir: Path) -> None:
        # A failure that left a run directory behind is told what failed and where the
        # evidence is, not just one sentence (register G10). The reason keeps the
        # failure role and the ``ralph: `` voice the one-line handler used, so the
        # existing stderr assertions on it still hold; the next step names the run
        # directory itself — the retained diagnostics, and the backend's stream when a
        # session got that far — so the block carries the evidence path on its own.
        self._message("failure", reason)
        self._message(
            "chrome",
            f"next step: inspect the retained diagnostics under {run_dir}, then run "
            "Ralph again once the cause is resolved",
        )

    def failed(self, message: str) -> None:
        self._finalize()
        self._message("failure", message)

    def _fact(self, label: str, value: str, *, keep_tail: bool = False) -> None:
        """One header fact, fitted to the window. The label rides in the chrome, so
        shortening a long value can never cost the operator the name of the field
        they are looking at."""
        prefix, value = fit_line(
            redact(f"{PREFIX}{label} "), redact(value), self._width(), keep_tail=keep_tail
        )
        self._paint(prefix, "chrome", value, "content")

    def _content(self, text: str) -> None:
        """A line of Backend content Ralph is quoting — the concluding message. The
        ``ralph: `` prefix rides in the chrome so the line stays greppable and part of
        the outcome block, while the value is the uncoloured ``content`` role so
        Backend text can never be recoloured into Ralph's own voice (register G12).
        Fitted to the window like a header fact: it is display, not a greppable event
        name, and the whole of it survives on disk (register G18)."""
        prefix, value = fit_line(redact(PREFIX), redact(text), self._width(), keep_tail=False)
        self._paint(prefix, "chrome", value, "content")

    def _message(self, role: str, text: str) -> None:
        """A whole line emitted unfitted: a warning, a failure, or the piped
        Iteration rule that has no window to fit to. A warning's or a failure's
        wording is what the operator greps for and what register G19 pins
        byte-identical, so a narrow window may wrap one; it may not clip words out of
        it. Only the header and outcome facts trade characters for a window (register
        G3), and only because every fact they drop is recoverable from the run
        directory they name."""
        self._paint("", role, redact(PREFIX + text), role)

    def _line(self, role: str, text: str) -> None:
        """A whole operator-facing line emitted verbatim, without the ``ralph: ``
        prefix the header uses. The handoff block and the deviation warnings carry
        their own first-column vocabulary -- ``RALPH NEEDS OPERATOR``, ``reason:``,
        ``manual resume:``, ``continue Ralph:``, ``WARNING:`` -- that operators and
        tooling anchor on, so the prefix that marks Ralph's header voice would break
        those anchors. Redacted at the choke point and painted whole in *role*; a long
        line wraps rather than clipping, exactly like a warning or a failure (register
        G17/G19)."""
        self._paint("", role, redact(text), role)

    def _paint(self, prefix: str, prefix_role: str, value: str, value_role: str) -> None:
        # The last stop before the stream. Everything above has already passed
        # through `redact`, the single choke point for operator-facing output
        # (register G17), and truncation follows redaction there so the placeholder
        # is what gets measured rather than the secret it replaced.
        if self._colour():
            if prefix and PALETTE[prefix_role]:
                prefix = f"{PALETTE[prefix_role]}{prefix}{RESET}"
            if value and PALETTE[value_role]:
                value = f"{PALETTE[value_role]}{value}{RESET}"
        self._emit_raw(prefix + value)

    def _emit_raw(self, text: str) -> None:
        # Every operator line goes through here so the status line is erased before it
        # and redrawn after it, and so the ticker cannot interleave a repaint with it
        # (register G3). The erase and redraw are no-ops when nothing is painted or the
        # stream is not a terminal, so an ordinary piped log is unchanged.
        with self._lock:
            self._erase_status_locked()
            self._stream.write(text + "\n")
            self._stream.flush()
            self._paint_status_locked()

    def _begin_iteration(self, number: int, iterations: int) -> None:
        with self._lock:
            self._status_active = True
            self._status_established = False
            self._iteration = number
            self._iterations = iterations
            self._tool = None
            self._tool_count = 0
            self._context = None
            self._subagents = 0
            self._iteration_start = time.monotonic()

    def _establish_locked(self) -> None:
        # The first Observation of an Iteration turns the status line on and starts
        # the ticker that keeps its clock moving; later Observations only repaint it.
        if not self._status_established:
            self._status_established = True
            # Anchor the heartbeat clock so the first append-only heartbeat is a full
            # slow interval away, never one the instant progress begins.
            self._last_heartbeat = time.monotonic()
            self._start_ticker_locked()
        if self._is_terminal():
            self._erase_status_locked()
            self._paint_status_locked()

    def _finalize(self) -> None:
        # End the status lifecycle: erase any painted line and stop the ticker. Join
        # the ticker outside the lock, since the ticker takes the lock to repaint.
        with self._lock:
            self._status_active = False
            self._status_established = False
            self._erase_status_locked()
            stop, thread = self._ticker_stop, self._ticker_thread
            self._ticker_stop = self._ticker_thread = None
        if stop is not None:
            stop.set()
        if thread is not None:
            thread.join(timeout=2)

    def _start_ticker_locked(self) -> None:
        if self._ticker_thread is not None:
            return
        self._ticker_stop = threading.Event()
        self._ticker_thread = threading.Thread(
            target=self._run_ticker, args=(self._ticker_stop,), daemon=True
        )
        self._ticker_thread.start()

    def _run_ticker(self, stop: threading.Event) -> None:
        while not stop.wait(STATUS_TICK_SECONDS):
            try:
                self._tick()
            except (ValueError, OSError):
                # The stream was closed under us; stop rather than raising on a
                # daemon thread the run no longer depends on.
                return

    def _tick(self) -> None:
        with self._lock:
            if not (self._status_active and self._status_established):
                return
            if self._is_terminal():
                self._erase_status_locked()
                self._paint_status_locked()
            else:
                # No in-place line off a terminal: the clock degrades to a *slow*
                # append-only heartbeat carrying the same facts and no ANSI (G3/G11).
                # The ticker still wakes each second for a terminal's smooth clock, so
                # throttle the heartbeat to its own coarser cadence here.
                now = time.monotonic()
                if now - self._last_heartbeat >= HEARTBEAT_SECONDS:
                    self._last_heartbeat = now
                    self._stream.write(redact(self._status_text()) + "\n")
                    self._stream.flush()

    def _status_text(self) -> str:
        elapsed = (
            time.monotonic() - self._iteration_start
            if self._iteration_start is not None
            else 0.0
        )
        return render_status(
            self._iteration,
            self._iterations,
            elapsed,
            self._tool,
            self._tool_count,
            self._context,
            self._subagents,
            self._width(),
        )

    def _paint_status_locked(self) -> None:
        # Repaint the in-place status line, but only on a terminal, only while the
        # Iteration is live, and only once an Observation has established it. Uses a
        # carriage return, never a cursor-control escape, so ``NO_COLOR`` on a terminal
        # still emits no escape; colour is added only when colour is enabled.
        if not (self._status_active and self._status_established and self._is_terminal()):
            return
        text = self._status_text()
        self._painted_width = len(text)
        painted = f"{PALETTE['chrome']}{text}{RESET}" if self._colour() else text
        self._stream.write("\r" + painted)
        self._status_painted = True
        self._stream.flush()

    def _erase_status_locked(self) -> None:
        if not self._status_painted:
            return
        # Overwrite the painted characters with spaces and return to column zero, so
        # the next line starts clean without any cursor-control escape.
        self._stream.write("\r" + " " * self._painted_width + "\r")
        self._status_painted = False
        self._stream.flush()

    def _is_terminal(self) -> bool:
        try:
            return self._stream.isatty()
        except (AttributeError, ValueError):
            # A detached or closed stream is not a terminal; it must not be able to
            # stop the run by refusing to answer.
            return False

    def _colour(self) -> bool:
        # NO_COLOR disables colour when present and non-empty, per the convention.
        return self._is_terminal() and not os.environ.get("NO_COLOR")

    def _width(self) -> int | None:
        if not self._is_terminal():
            return None
        try:
            columns = os.get_terminal_size(self._stream.fileno()).columns
        except (AttributeError, OSError, ValueError):
            return FALLBACK_TERMINAL_WIDTH
        # Clamp a nonsense answer (a terminal that reports no size at all) rather
        # than truncating every line to nothing.
        return columns if columns > 0 else FALLBACK_TERMINAL_WIDTH
