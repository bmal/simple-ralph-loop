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


class RunConsole(Protocol):
    """The operator-facing seam. It widens as later tickets migrate their emit
    sites; it is structural, matched with no runtime class or ABC machinery, in the
    style of the Backend Protocol."""

    def run_started(self, settings: RunSettings) -> None: ...

    def interactive_only_resolved(self, label: str, issues: list[int]) -> None: ...

    def iteration_started(self, number: int, iterations: int) -> None: ...

    def trust_boundary_established(self, properties: list[str]) -> None: ...

    def iteration_finished(self, outcome: IterationOutcome) -> None: ...

    def run_finished(self, summary: RunSummary) -> None: ...

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

    def iteration_started(self, number: int, iterations: int) -> None:
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
        # Ring the bell on a terminal only, so an operator who walked away is called
        # back on every terminal outcome and a piped log carries no control character.
        if self._is_terminal():
            self._stream.write(BELL)
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

    def failed(self, message: str) -> None:
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
        self._stream.write(prefix + value + "\n")
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
