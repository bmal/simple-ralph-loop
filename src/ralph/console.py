"""The Run console: the one module permitted to address the operator, and the
rendering apparatus — palette, terminal detection, dynamic width, no-wrap
truncation, and the redaction choke point — that sits behind its small interface.

Invariants:
- ``RunConsole`` is the abstraction the rest of Ralph depends on; ``StreamRunConsole``
  is the concrete renderer and ``cli`` is the only module that constructs one
  (register G16). The Loop and the Backend adapters hand over value objects and
  plain facts and never construct operator-facing text (register G14).
- Every string a *Backend* authored passes through ``neutralise`` before it is
  redacted and rendered (register J1/J2). Text the Backend wrote is not text this
  module may render unexamined: whitespace controls become one space, whole CSI and
  OSC sequences go, and every remaining ``Cc``/``Cf`` codepoint goes -- so a piped log
  really does carry no ANSI and no bell, a concluding message cannot scroll the
  terminal up and erase the deviation warning above it, no field can paint a second
  row, and no ``\\n`` can forge a first-column anchor inside the handoff banner. The
  covered fields are the concluding message and session id, ``ToolObserved.name``,
  ``StageObserved.label``, ``MarkerWithdrawn.quote``, ``UnmarkedQuestion.quote``,
  ``KilledTask.task_id``, ``OperatorHelp.reason``/``.detail``/``.session_id``,
  ``failure_help``'s reason, and the feed's speaker and text. Neutralisation runs
  *before* redaction, never after, so a secret a Backend spliced a control character
  into is rejoined and then matched -- the retained artifacts close the same evasion
  through the matcher itself, not through this.
- Every operator-facing string this module emits passes through ``redact`` (register
  G17): operator lines in ``_write``, and the status line in ``_status_text``, which
  redacts its two Backend-authored fields before the width fitting measures them so a
  placeholder can never be clipped back into a secret. The status line has its own
  choke point rather than sharing ``_write`` because it is painted, not written --
  ``\\r`` and spaces, no trailing newline -- but nothing reaches a stream without one
  of the two. Retained
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
- Every measurement of what fits the window is in display columns, never in
  codepoints, and goes through the one ``display_width`` they share -- the header's
  ``fit_line``, the Iteration rule's fill, the status line's ``render_status``, and
  the run of spaces that erases the status line. ``summarize_message``'s cap on a
  concluding message is not one of them and stays a character budget: it bounds how
  much of a message the outcome block quotes, which ``fit_line`` then fits.
  A path of CJK directory names counts 34 codepoints and occupies about 50 columns,
  which is how a header fact register G3 pins as fitted-never-folded came to wrap and
  how the erase came to leave the tail of a wide status line on the screen. The
  measurement is ``unicodedata`` and nothing else -- the package has no runtime
  dependency and keeps none -- so it is per-codepoint and not grapheme-correct: East
  Asian ``W`` and ``F`` count two, combining marks count none, and an emoji ZWJ
  sequence over-measures. Over-measuring shortens a line sooner than it had to be;
  under-measuring wraps the header, which is the failure that matters.
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
  path including success (register G9). The Iteration's outcome is rendered as the
  Loop names it, because it already names what happened to that Iteration; only the
  *run*'s outcome is worded, through ``OUTCOME_HEADLINES``. Every outcome a run can
  reach is worded there — a structural test reads the vocabulary back out of the
  source, so a new one cannot silently degrade to the ``run ended: …`` fallback as
  ``interrupted`` did. The block prints on every terminal path, the ones that raise
  included; those carry no concluding message because their path produced none. The
  bell rings on every terminal outcome but only on a terminal, so a piped log
  carries no bell character (register G12): in ``run_finished`` for every path that
  reached a run directory, and in ``failed`` for the one-line argument and
  precondition path, which carries a ``RalphError`` from the git context, the prompt,
  the worktree lock, and a failed ``resume`` handover as well as a bad flag. The
  concluding message is truncated for display only — nothing is written to disk from it, so the retained
  copy stays byte-identical (register G18).
  It arrives as the Backend's prose: this module holds no knowledge of the Loop
  protocol and never decides what a marker is, so the Loop drops the protocol's own
  marker declarations before handing it over (the ratified owner decision on #58).
  The Trust boundary line (``trust_boundary_established``) is emitted where its proof
  completes, after the first Iteration's preflight, never in the header (register G8).
- The two commands that are not ``run`` address the operator here too (register G22).
  ``state_removed`` reports what ``clean`` destroyed and, on its own line ahead of the
  fitted path, whether there was anything there at all, so a command that irreversibly
  deletes every run's evidence can never be mistaken for a no-op. ``confirm_removal``
  puts the same two facts to the operator *before* any of it goes, worded from the same
  ``CleanOutcome`` so the number agreed to is the number destroyed, and
  ``removal_declined`` reports the refusal in the same ``removed``-first shape rather
  than borrowing the wording of a worktree that held nothing. Asking is all the console
  does: reading the answer is ``cli``'s, because this module writes to an operator and
  does not interview them (decision J4). ``resume_started``
  is recovery's header: the session being entered, the trust boundary re-proven, and
  the host-isolation status stated either way — never reported by omission. Only the
  standing full-auto caveat follows it, and then ``resume`` replaces its own process,
  so nothing can be rendered after the handover.
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
  types -- progress facts (``StageObserved``, ``ToolObserved``, ``ContextObserved``,
  ``SubagentsObserved``), the mid-run warnings an adapter used to print itself
  (``MarkerWithdrawn``,
  ``UnmarkedQuestion``, ``KilledTask``), and the Backend's running commentary
  (``Narrated``, ``ToolActivity``, ``StepObserved``); the adapter emits facts and the
  console decides wording, so the load-bearing ``withdrew it``, ``unmarked
  operator-directed``, and the killed-task phrase live here now, not in the adapter
  (register G19). The progress facts feed a single status line that repaints in
  place on a terminal
  (register G3/G4): Iteration and budget, the Iteration's elapsed time, what the
  Backend is doing, the tool count, the orchestrator's live context size, and the live
  subagent count -- each field absent, never zero, until a Backend supplies it, so an
  OpenCode run (which reports no roster) simply carries no subagent field rather than
  a fabricated zero (register G4/G5). One field answers what the Backend is doing, and
  the Stage it declared through the Loop protocol is the better answer than the tool
  whenever there is one -- it names where the Backend is in the operator's own prompt.
  The console never infers a Stage from the tool mix, and stops asserting one that has
  gone stale (``STAGE_STALE_SECONDS`` since it was declared), giving the field back to
  the last tool, so a Backend that declared a Stage and then forgot to announce the
  transition degrades to a lower-confidence truth rather than a confident untruth
  (register G6). Its ticking elapsed clock is the only motion -- a background ticker
  repaints it on a cadence so a long silent tool call still advances the clock, and a
  frozen clock therefore means stalled rather than a spinner spinning over a hang.
  One Iteration's ticker cannot survive into the next: ``_finalize``'s wait for it is
  bounded (``TICKER_JOIN_SECONDS``) so a stuck repaint never holds up the outcome
  block, and a ticker still in flight when that wait expires reads the console's
  cleared stop event as its own supersession and retires instead of repainting
  alongside its successor.
  The line is read against the terminal's live width and never wraps: it drops fields
  right-to-left (``render_status``) and clips rather than folding, in columns rather
  than codepoints. It is established when the Iteration opens, not by its first
  Observation, so the clock ticks from zero for the whole of an Iteration a Backend
  reports nothing about: no clock at all is a third state beside running and stalled
  that US7 does not account for, and liveness is the one thing the line owes an
  operator unconditionally. Only the fields wait for facts, the running tool count
  among them -- absent until a Backend names a tool, like the context gauge and the
  roster, so the first paint of an Iteration is a bare Iteration and clock rather than
  a ``0 tools`` nothing reported (register G4/G5). The status line is erased before
  any other operator line prints and redrawn after, so a warning or a header fact never corrupts it. Off a
  terminal there is no in-place line: the ticker degrades to slow append-only
  heartbeat lines carrying the same facts and no ANSI, so a piped log stays clean
  (register G3/G11). The status apparatus uses only ``\\r`` and spaces to repaint, never
  cursor-control escapes, so ``NO_COLOR`` on a terminal still emits no escape at all.
- The commentary Observations are the opt-in Backend feed and nothing else. With no
  *feed* stream they are dropped, which is what makes the default view a dashboard
  (register G2); with one they are written to it -- stdout in production, while the
  dashboard keeps stderr, so redirecting either leaves the other on the terminal
  (register G11). Every feed line opens with the speaker that produced it, the
  Backend or one specific subagent, so concurrent monologues stay attributable. A
  streamed fragment leaves its speaker's line open until the text completes it, so a
  prefix only ever lands at the start of a line; ``_finalize`` closes any line still
  open when the Iteration ends. Ralph paints no colour onto the feed at all, on a
  terminal or off it (register G12), so nothing Ralph writes there can be read as its
  own voice; Backend text passes through the same redaction choke point as every
  other operator-facing string (register G17), and through ``neutralise_feed``, which
  keeps the Backend's own SGR colour and its tabs and removes everything else. That
  narrows the verbatim pass-through the feed was given when the commentary left the
  default view: a feed line erases and redraws the status line around itself, and an
  un-redirected ``--verbose`` puts both streams on one terminal, so cursor motion or a
  carriage return here owns the operator's screen and escapes the row its speaker
  prefix names it on. Colour and indentation are what make the raw transcript worth
  asking for and can do neither, so they are what stays.
- *quiet* drops exactly the status line and the Iteration blocks (register G11). The
  header, the Trust boundary, the resolved children, the run summary, the deviation
  warnings, the mid-run warnings, and every failure block still print, so an
  unattended run is quiet without the operator losing their help. With no status line
  there is no clock to keep moving, so the ticker never starts and no heartbeat
  appends either. *quiet* and *feed* are orthogonal: they govern different streams.
- What this module knows of the Backend is its *name*, and only where the operator is
  being told one: the header states it, the feed prefixes every line with it so
  concurrent monologues stay attributable, and ``IN_SCOPE_BACKEND_DEVIATIONS`` selects
  the deviation wording for a declared In-scope backend -- a warning about which
  subscription credential a run may read cannot avoid naming the Backend it belongs
  to. That is a string this module renders, not a Backend it dispatches on: nothing
  here branches on how a Backend behaves, reads a Backend's stream shape, or asks what
  an adapter did, so a third Backend adds a deviation entry to that table and nothing
  else. The name arrives from the run header (``UNNAMED_BACKEND`` until it does), which
  is the same one-way flow every other fact it renders takes.

Depends on / must not know: ``redaction`` (functions only, never the active-redactor
global). It must not know how a run is driven, how any Backend behaves, or what any
value object it renders was computed from -- it holds a Backend's *name*, as the
invariant above records, and nothing else about it.

See also: ``cli`` (the composition root; constructs the concrete console, passes it
in, and drives it directly for ``clean`` and ``resume``), ``loop`` (emits the run's
facts), CONTEXT.md (**Run console**).
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import threading
import time
from typing import Protocol, TextIO
import unicodedata

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

# The Backend feed's speaker prefix, and the separator that names one subagent
# apart from the Backend that launched it (register G11). Every feed line carries
# one, so a run under ``--unsafe-allow-agents`` -- where the Backend and each of
# its subagents narrate at once -- stays attributable instead of braiding three
# monologues into one. It is deliberately not ``PREFIX``: the feed is Backend
# content, and nothing on it may read as Ralph's own voice (register G12).
FEED_SUBAGENT_SEPARATOR = "/"
FEED_SPEAKER_DELIMITER = ": "
# What the feed calls the speaker before a run header has named the Backend: a real
# run always states one first, so this stands in only for a console driven without it.
UNNAMED_BACKEND = "backend"

# Used only when the stream claims to be a terminal but refuses to report a usable
# size; a real ioctl answer is always preferred, and a non-terminal has no width.
FALLBACK_TERMINAL_WIDTH = 80
ELLIPSIS = "..."

# The terminal bell, rung on every terminal outcome (register G12) — on a terminal
# only, so a piped or redirected log never carries the control character.
BELL = "\a"

# The introducers a Backend-authored string is parsed against before it is rendered
# (register J1). ``ESCAPE`` opens a seven-bit sequence; ``C1_CSI`` and ``C1_OSC`` are
# the eight-bit single-codepoint equivalents a terminal obeys just as readily, and
# which a check written against ``\033`` alone does not see at all.
ESCAPE = "\033"
C1_CSI = "\x9b"
C1_OSC = "\x9d"
STRING_TERMINATOR = "\x9c"

# The whitespace controls, mapped to a single space *before* anything is removed, so
# deleting a control can never fuse two words into one (register J1): a tool name
# ``Bash\nrm -rf /`` becomes ``Bash rm -rf /``, never ``Bashrm -rf /``.
WHITESPACE_CONTROLS = "\n\r\t\v\f"

# Select Graphic Rendition: the one CSI the opt-in Backend feed keeps, identified by
# its final byte. Colour is what makes a transcript a transcript; cursor motion is
# what would let the Backend own the operator's screen.
SGR_FINAL = "m"

# The concluding message is truncated for display so a long final narration does not
# fill the outcome block; the retained artifacts keep the whole of it (register G18).
CONCLUDING_MESSAGE_LIMIT = 200

# The run summary's headline per terminal outcome. ``budget_exhausted`` keeps the
# byte-identical ``iteration budget exhausted`` phrase an operator greps for
# (register G19); an unknown outcome degrades to naming itself rather than vanishing.
# Every outcome a Backend adapter or the Loop can reach is worded here -- a
# structural test reads the vocabulary back out of the source, so the next one
# cannot fall through to naming itself unnoticed as ``interrupted`` did. An
# interruption is worded as a stop rather than a failure: the operator asked for it.
OUTCOME_HEADLINES = {
    "complete": "run complete",
    "budget_exhausted": "run incomplete: iteration budget exhausted without completion",
    "needs_input": "run handed off for operator input",
    "interrupted": "run stopped: interrupted by the operator",
    "backend_contract_failure": "run failed: backend contract violation",
    "backend_failure": "run failed: backend error",
    "timeout": "run failed: iteration timed out",
}

# What a preflight proves, named once. A run states these after its first Iteration's
# preflight clears and recovery re-states them before it hands over, so the two sites
# name the same properties instead of drifting into two vocabularies for one proof.
# Host isolation is not among them: it is proven elsewhere (the sandbox self-test) and
# is the one property an operator can relax, so each site reports it in the way that
# site can -- the run by including it in the proven list, recovery on a line of its own
# that is printed whether or not it holds.
PREFLIGHT_PROPERTIES = ("subscription-only authentication", "customization isolation")
HOST_ISOLATION_PROPERTY = "host isolation"

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
IN_SCOPE_CLAUDE_DEVIATION = "in-scope-claude"
IN_SCOPE_OPENCODE_DEVIATION = "in-scope-opencode"

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
    IN_SCOPE_CLAUDE_DEVIATION: (
        "WARNING: --in-scope-backend claude is set; this run can read and write the "
        "Claude subscription store. sandbox-exec confines the whole process tree at "
        "once, so every command in the run can read that credential, not only the "
        "Claude session. No other guarantee is relaxed."
    ),
    IN_SCOPE_OPENCODE_DEVIATION: (
        "WARNING: --in-scope-backend opencode is set; this run can read the OpenCode "
        "subscription credential and rewrite it on token refresh. sandbox-exec "
        "confines the whole process tree at once, so every command in the run can "
        "read that credential, not only the OpenCode session. No other guarantee is "
        "relaxed."
    ),
}

# Which deviation names a declared in-scope backend. The Loop and ``cli`` resume
# name the backend; the wording stays here (register G14).
IN_SCOPE_BACKEND_DEVIATIONS = {
    "claude": IN_SCOPE_CLAUDE_DEVIATION,
    "opencode": IN_SCOPE_OPENCODE_DEVIATION,
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
    """How one Iteration closed: its number and the budget it belongs to, how long the
    Iteration ran, what happened to it, the session id to resume, and the Backend's
    concluding prose. The Loop fills it with facts; the console truncates the message
    and words the block (register G14).

    ``outcome`` names what happened to *this Iteration* and is rendered as it stands:
    the run-level word is the Loop's own and is worded by ``OUTCOME_HEADLINES``
    instead (register J3). ``session_id`` is absent when an Iteration stopped before
    any arrived, and ``concluding_message`` when the path that ended it produced
    none -- every raising path. It is prose: the Loop has already dropped the
    protocol's own marker declarations, which this module knows nothing about."""

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
class CleanOutcome:
    """What ``clean`` destroyed: the state directory it was pointed at, and how many
    runs' evidence went with it. ``runs`` is counted before the tree is deleted, so the
    report names what was destroyed rather than what is left, and ``None`` says there
    was nothing there to destroy at all — the one distinction an operator cannot afford
    to have blurred, so it is a state the value cannot fail to carry rather than a
    second field that means nothing half the time. ``cli`` supplies the facts, the
    console words them (register G14/G22)."""

    state_root: Path
    runs: int | None


@dataclass(frozen=True)
class ResumeSettings:
    """The compact header ``resume`` prints before it replaces its own process with
    the interactive session: which session the operator is entering, and what the
    handover re-proved. ``reproven`` names the properties preflight established;
    host isolation rides its own flag because it is the one the operator can relax,
    and it is stated either way rather than reported only by its absence (register
    G8/G22)."""

    backend: str
    model: str
    session_id: str
    host_isolated: bool
    reproven: tuple[str, ...]


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
    byte-identical (register G14/G19). ``reason``, ``detail`` and ``session_id`` are
    Backend-authored and are neutralised to one line each before the block is rendered
    (register J1): the newlines a Backend put in them otherwise opened forged
    ``manual resume:`` and ``continue Ralph:`` lines *above* the genuine ones, inside
    the very block whose purpose is to hand the operator a command to run."""

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
class StageObserved:
    """The Backend declared which stage of the operator's own prompt it has reached,
    through the Loop protocol (register G6). Never inferred from tool use: the stages
    live in the prompt, which Ralph snapshots but never reads, so a guess would
    confidently report the wrong one. ``label`` is the free text the protocol accepts,
    already bounded by the parser, which refuses outright a label carrying a control
    character rather than repairing one. The console neutralises and redacts whatever
    reaches its sink anyway (register J1/J2), because it must not know what any value
    object it renders was computed from -- the guarantee is over its own inputs, not
    over an upstream module staying careful. It decides when the Stage has gone
    stale."""

    label: str


@dataclass(frozen=True)
class ToolObserved:
    """The orchestrator reached for a tool. ``name`` is the tool the status line
    shows as the current or last tool, and each one increments the tool count. It is
    Backend-authored, so the console neutralises it before rendering (register J1): a
    name carrying a newline would otherwise paint a status line two rows tall, of
    which the erase clears one."""

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
    orchestrator waiting on them; the descriptions leave room for a later view. Emitted
    only by a Backend that has a roster to report (Claude): a Backend with no subagent
    stream at all (OpenCode) never sends this, and the count stays absent rather than a
    fabricated zero (register G5)."""

    roster: tuple[str, ...]


@dataclass(frozen=True)
class MarkerWithdrawn:
    """The Backend raised a needs-input marker in a message other than the one the
    Iteration was judged on, then spoke past it: a question it withdrew. ``quote`` is
    the raw fragment; the console neutralises, redacts, bounds, and words it -- in that
    order (register J1/J2, G17/G19)."""

    quote: str


@dataclass(frozen=True)
class UnmarkedQuestion:
    """The Backend's final message ended on an operator-directed question with no
    marker: a low-confidence signal the run warns on and continues past. ``quote`` is
    the raw fragment the console neutralises, redacts, bounds, and words -- in that
    order (register J1/J2, G17/G19)."""

    quote: str


@dataclass(frozen=True)
class KilledTask:
    """The Backend's runtime reported it killed a background task the Backend left
    running when it ended its turn. ``task_id`` is the CLI-generated identifier the
    console names the abandoned task by -- Backend-authored, so neutralised before it
    is rendered (register J1) -- or ``None`` when the report carried none."""

    task_id: str | None


@dataclass(frozen=True)
class Narrated:
    """A passage of running narration from the Backend or one of its subagents --
    the commentary that left the default view (register G2) and returns only under
    the opt-in feed. ``subagent`` is ``None`` for the Backend's own words and the
    identifier of the specific subagent otherwise, so three concurrent monologues
    stay attributable instead of braiding into one. ``partial`` says the passage is
    a fragment of a message still streaming rather than a whole one, so the console
    holds the line open instead of ending it -- the difference between a Backend
    that emits complete messages and one that emits deltas. ``text`` reaches the feed
    through ``neutralise_feed``, which keeps the Backend's own colour and indentation
    and removes what would let it leave its row."""

    text: str
    subagent: str | None = None
    partial: bool = False


@dataclass(frozen=True)
class ToolActivity:
    """A tool-use update as the Backend's stream reported it, for the feed alone:
    every state change, not one per call, and a subagent's as well as the
    orchestrator's. It is deliberately not ``ToolObserved`` -- that one is the
    status line's orchestrator-only, one-per-call fact, and counting these would
    inflate it. ``state`` is the status the stream reported, absent when it named
    none."""

    name: str
    subagent: str | None = None
    state: str | None = None


@dataclass(frozen=True)
class StepObserved:
    """A step boundary in a Backend stream that has them (OpenCode's step-start and
    step-finish parts), for the feed alone. The word names that Backend's own events
    and is reserved to them, which is why register G23 keeps the Stage vocabulary off
    it."""

    started: bool


Observation = (
    StageObserved
    | ToolObserved
    | ContextObserved
    | SubagentsObserved
    | MarkerWithdrawn
    | UnmarkedQuestion
    | KilledTask
    | Narrated
    | ToolActivity
    | StepObserved
)


# The status line's fields, in left-to-right priority order (register G4). It drops
# from the right as the window narrows, so the Iteration and elapsed always survive
# and the subagent count is the first to go.
STATUS_SEPARATOR = " · "
# How often the background ticker repaints the in-place status line so its elapsed
# clock keeps ticking through a long silent tool call -- the one motion that
# distinguishes a live run from a hang (register G3).
STATUS_TICK_SECONDS = 1.0
# How long a declared Stage is believed before the status line stops asserting it and
# falls back to the last tool (register G6). The Backend declares a Stage as it enters
# one; the failure this guards is the Backend that declares once and never announces a
# transition, leaving a four-hour Iteration claiming it is still selecting a task. A
# fixed bound, deliberately not scaled to the run's ``--timeout``: the failure only
# bites a long Iteration, and on a short one a Stage declared at the top is still
# plausibly true when the Iteration ends. Fifteen minutes is long enough that a real
# transition beats it by a wide margin and short enough that a forgotten one stops
# misleading well inside a long Iteration. Falling back loses information but never
# states an untruth, which is the trade register G6 makes.
STAGE_STALE_SECONDS = 900.0
# How long ``_finalize`` waits for the Iteration's ticker to retire before moving on.
# Bounded on purpose -- a stuck repaint must not hold up the outcome block an operator
# is waiting for -- which is exactly why giving up has to be safe: a ticker still in
# flight when the wait expires checks that it is still the live one before it paints,
# and retires when a successor has taken over (finding 16 of the #36 review).
TICKER_JOIN_SECONDS = 2.0
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
    tool_count: int | None,
    context_tokens: int | None,
    subagents: int | None,
    width: int | None,
    *,
    stage: str | None = None,
    stage_age_seconds: float | None = None,
) -> str:
    """Render the status line, dropping fields right-to-left until it fits *width*
    and never wrapping (register G3/G4). A stream with no width (anything that is not
    a terminal) keeps every field. The Iteration and elapsed are never dropped; when
    even they do not fit the line is clipped rather than folded onto a second row.

    A field the Backend never supplied is absent, not zero: ``tool_count``,
    ``context_tokens`` or ``subagents`` of ``None`` drops that field entirely rather
    than asserting a zero that reads as a fact (register G4/G5). A genuine count of
    zero -- a Backend that reported an empty subagent roster -- still renders, so the
    two are not conflated. OpenCode emits no subagent roster, so its status line
    simply carries no subagent field, while a Claude run shows the count once its
    roster event supplies one. The line is established when the Iteration opens, so
    its very first paint is a bare Iteration and clock: a tool count of ``None`` is
    what keeps that paint from opening on a ``0 tools`` no Backend has reported.

    One field answers "what is it doing", and the declared Stage is the better answer
    than the tool whenever there is one: it names where the Backend is in the
    operator's own prompt rather than which file it happened to open. It gives that
    field back once it has gone stale -- ``stage_age_seconds`` past
    ``STAGE_STALE_SECONDS`` -- so a Backend that declared a Stage and then forgot to
    announce the transition degrades to the lower-confidence but always-true last tool
    rather than going on asserting something untrue (register G6). An age of ``None``
    is an undeclared age, not an infinite one, and reads as fresh."""
    fields = [f"iteration {iteration}/{iterations}", format_duration(elapsed_seconds)]
    stale = stage_age_seconds is not None and stage_age_seconds > STAGE_STALE_SECONDS
    activity = stage if stage and not stale else tool
    if activity:
        fields.append(activity)
    if tool_count is not None:
        fields.append(f"{tool_count} tool{'' if tool_count == 1 else 's'}")
    if context_tokens is not None:
        fields.append(f"context {context_tokens} tokens")
    if subagents is not None:
        fields.append(f"{subagents} subagent{'' if subagents == 1 else 's'}")
    # Drop from the right (subagents first) until the prefixed line fits the window,
    # measured in columns so a wide Stage label cannot push the line onto a second row.
    while len(fields) > 2 and width is not None and (
        display_width(PREFIX) + display_width(STATUS_SEPARATOR.join(fields)) > width
    ):
        fields.pop()
    text = PREFIX + STATUS_SEPARATOR.join(fields)
    if width is not None and display_width(text) > width:
        # Even the Iteration and elapsed overflow a very narrow window: clip rather
        # than fold, so the line still occupies exactly one row.
        text = _clip_columns(text, width)
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

    def resume_started(self, settings: ResumeSettings) -> None: ...

    def confirm_removal(self, outcome: CleanOutcome) -> None: ...

    def removal_declined(self, outcome: CleanOutcome) -> None: ...

    def state_removed(self, outcome: CleanOutcome) -> None: ...

    def relaunching_full_auto(self) -> None: ...

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


def _csi_end(text: str, start: int) -> tuple[int, str | None]:
    """Where the CSI sequence whose parameter bytes begin at *start* ends, and the
    final byte that named it.

    A sequence with no final byte in the string is not a sequence: *start* is returned
    with ``None`` so the caller drops the introducer alone. Consuming to the end of the
    string instead would let one truncated escape swallow the rest of what the operator
    was going to be told (register J6)."""
    index = start
    while index < len(text) and 0x30 <= ord(text[index]) <= 0x3F:
        index += 1
    while index < len(text) and 0x20 <= ord(text[index]) <= 0x2F:
        index += 1
    if index < len(text) and 0x40 <= ord(text[index]) <= 0x7E:
        return index + 1, text[index]
    return start, None


def _osc_end(text: str, start: int) -> int | None:
    """Where the OSC string whose payload begins at *start* ends -- at a bell, a string
    terminator, or ``ESC \\`` -- or ``None`` when it is unterminated, for the same
    reason ``_csi_end`` refuses a truncated sequence."""
    index = start
    while index < len(text):
        char = text[index]
        if char in (BELL, STRING_TERMINATOR):
            return index + 1
        if char == ESCAPE and text[index + 1 : index + 2] == "\\":
            return index + 2
        index += 1
    return None


def _neutralise(text: str, *, feed: bool) -> str:
    """The neutraliser both policies share (register J1).

    Two passes, in the order the register fixes. The whitespace controls become a
    single space first, so nothing removed afterwards can fuse the words they
    separated. Then one walk takes whole CSI and OSC sequences -- the whole sequence, so
    ``\\033[31mRED`` becomes ``RED`` and never the orphaned ``[31mRED``. Every remaining
    ``Cc``/``Cf`` codepoint goes last, one codepoint at a time.

    That last part is deliberately narrow: a lone ``ESC`` takes only itself, never the
    character behind it, even though a terminal would read the pair as a two-character
    escape. Eating that character would break a secret a Backend spliced an ``ESC``
    into back into two fragments that ``redact`` cannot match -- which is the evasion
    register J2 exists to close. Dropping the introducer alone already leaves the
    sequence inert, and it leaves the secret whole for the matcher."""
    if not text:
        return text
    text = "".join(
        char if char == "\t" and feed else " " if char in WHITESPACE_CONTROLS else char
        for char in text
    )
    kept: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == ESCAPE and text[index + 1 : index + 2] == "[":
            end, final = _csi_end(text, index + 2)
            if final is None:
                index += 1
                continue
            if feed and final == SGR_FINAL:
                kept.append(text[index:end])
            index = end
            continue
        if char == C1_CSI:
            # The eight-bit CSI is never kept, on either stream: a Backend that wants
            # to colour its own feed has the seven-bit form, and this one is what a
            # guard written against ``\033`` misses.
            end, final = _csi_end(text, index + 1)
            index = end if final is not None else index + 1
            continue
        if (char == ESCAPE and text[index + 1 : index + 2] == "]") or char == C1_OSC:
            terminated = _osc_end(text, index + 2 if char == ESCAPE else index + 1)
            index = terminated if terminated is not None else index + 1
            continue
        if char == "\t" and feed:
            kept.append(char)
            index += 1
            continue
        if unicodedata.category(char) in ("Cc", "Cf"):
            index += 1
            continue
        kept.append(char)
        index += 1
    return "".join(kept)


def neutralise(text: str) -> str:
    """Backend-authored text made safe to render to an operator (register J1): no
    escape sequence, no bell, no format codepoint, and never more than one row.

    This is the boundary ``protocol.extract_stage`` already drew for the Stage marker,
    generalised to every other field a Backend authors that reaches the same terminal.
    It runs *before* ``redact`` at each choke point (register J2), so a secret with a
    control character spliced into it is rejoined and then matched. What it removes is
    control of the terminal, not what the Backend said: no line is dropped and no
    message is shortened (register J6). The one exception is an OSC payload -- a window
    title, a clipboard write -- which is an instruction addressed to the terminal
    rather than a word addressed to the operator, and goes with the sequence carrying
    it."""
    return _neutralise(text, feed=False)


def neutralise_feed(text: str) -> str:
    """The same neutraliser, relaxed for the opt-in ``--verbose`` feed: the Backend's
    own SGR colour and its tabs survive, and nothing else does.

    #42 passed the feed through verbatim and documented that; this narrows it. An
    un-redirected ``--verbose`` puts the feed on the same terminal as the dashboard, so
    a cursor-motion escape erases Ralph's own lines from here just as well as from a
    concluding message, and a feed line carrying a newline or a carriage return escapes
    the row its speaker prefix names it on. Colour and indentation are what make the
    raw transcript worth asking for and cannot do either, so they stay.

    Kept colour is closed at the end of the line. SGR is terminal state, not line
    state: a Backend that opens ``\\033[8m`` and never resets it would otherwise
    conceal every row that follows -- the next feed line, its own speaker prefix, and
    Ralph's status line repainted around it. Closing the row is what makes "its colour
    stays" mean the Backend's own line and not the operator's screen."""
    kept = _neutralise(text, feed=True)
    if ESCAPE not in kept:
        return kept
    # Only genuinely open colour is closed. A Backend that resets its own line is
    # left byte-for-byte as it wrote it, which is the whole of what #42 protected.
    last = kept[kept.rfind(ESCAPE) :]
    final = last.find(SGR_FINAL)
    closed = final >= 0 and last[: final + 1] in (RESET, f"{ESCAPE}[{SGR_FINAL}")
    return kept if closed else kept + RESET


def summarize_message(text: str) -> str:
    """Collapse a passage of Backend prose to one line and cap its length for the row
    it is shown on: the concluding message in an outcome block, and the withdrawn and
    unmarked fragments the two mid-run warnings quote, which is the whole of the
    bounding those warnings get (issue #40 moved it here, where a message already had
    to be bounded, and issue #61 removed the second copy it left behind in
    ``protocol``). Display only: the caller never writes this back to disk, so the
    retained artifacts keep the whole passage byte-identical (register G18).

    What arrives is prose, and it is already neutralised and redacted -- so the cap
    measures a ``[redacted]`` placeholder rather than the secret it replaced, and can
    never clip one back into a secret (register J2). The Loop protocol's own marker
    declarations are dropped upstream, where the protocol is known, because collapsing
    the message here destroys the line anchoring a declaration is matched on."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= CONCLUDING_MESSAGE_LIMIT:
        return collapsed
    return collapsed[: CONCLUDING_MESSAGE_LIMIT - len(ELLIPSIS)] + ELLIPSIS


def _char_columns(char: str) -> int:
    """How many terminal columns one codepoint occupies.

    Deliberately not grapheme-correct, and it does not pretend to be: it answers for
    codepoints, so a base character followed by two combining marks measures right
    while an emoji ZWJ sequence -- several wide codepoints a terminal may draw as one
    glyph -- measures wide. Both are the standard-library answer, which is the whole
    of what is available without a runtime dependency, and the failure it leaves is
    over-measuring (a line shortened sooner than it had to be) rather than the
    under-measuring that wraps a header or leaves erase residue on the screen."""
    if unicodedata.category(char) in ("Mn", "Me", "Cc", "Cf"):
        # A combining mark composes onto the character before it and takes no column
        # of its own; a control or format codepoint is not a column at all. Backend
        # text has already lost the latter at ``neutralise``, and Ralph writes none.
        return 0
    # ``W`` and ``F`` are the two East Asian classes that are unambiguously double
    # width. ``A`` (ambiguous) covers the box-drawing characters the Iteration rule
    # is built from and the middle dot the status line separates fields with, and is
    # single width in a terminal at its default settings, which is the only guess
    # available here and the one that leaves Ralph's own chrome measured as it is.
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def display_width(text: str) -> int:
    """How many terminal columns *text* occupies -- the one measurement of what fits
    the window, shared by ``fit_line``, ``render_status`` and the status line's erase,
    so a header fact, a status line, and the spaces that erase it can never disagree
    about how wide the same string is. (``summarize_message`` measures characters, not
    columns, on purpose: it caps how much of a message is quoted, and what it returns
    is fitted here afterwards.)

    Codepoint count is not column count: a path of CJK directory names counts 34
    codepoints and occupies about 50 columns, which is how a header fact register G3
    pins as fitted-never-folded came to wrap. See ``_char_columns`` for what this
    measures and what it does not."""
    return sum(_char_columns(char) for char in text)


def _fitting_length(text: str, columns: int, *, from_end: bool) -> int:
    """How many characters taken from one end of *text* fit *columns*. A double-width
    character that would straddle the last column is not taken rather than half-drawn,
    so the answer may be one column short of the budget."""
    if columns <= 0:
        return 0
    total = 0
    for taken, char in enumerate(reversed(text) if from_end else text):
        total += _char_columns(char)
        if total > columns:
            return taken
    return len(text)


def _clip_columns(text: str, columns: int) -> str:
    """The longest prefix of *text* that fits *columns*, never a codepoint's worth
    over."""
    return text[: _fitting_length(text, columns, from_end=False)]


def _clip_columns_tail(text: str, columns: int) -> str:
    """The longest *suffix* of *text* that fits *columns* -- the informative end of a
    path, where the leading directories are what can be spared.

    A suffix never opens on a zero-width codepoint: a combining mark belongs to the
    character it composes onto, and one whose base did not survive would compose onto
    the last dot of the ellipsis in front of it instead."""
    tail = text[len(text) - _fitting_length(text, columns, from_end=True) :]
    start = 0
    while start < len(tail) and _char_columns(tail[start]) == 0:
        start += 1
    return tail[start:]


def fit_line(prefix: str, value: str, width: int | None, *, keep_tail: bool) -> tuple[str, str]:
    """Shorten *value* so ``prefix + value`` occupies at most *width* columns.

    A stream with no width (anything that is not a terminal) is returned
    untouched. ``keep_tail`` preserves the informative end of a path — the run id,
    the file name — where the leading directories are what can be spared.

    Columns, not codepoints (``display_width``): the fields most likely to hold a
    wide character are the paths, and they are the ones fitted tail-first."""
    if width is None or display_width(prefix) + display_width(value) <= width:
        return prefix, value
    budget = width - display_width(prefix)
    marker = display_width(ELLIPSIS)
    if budget < marker + 1:
        # Narrower than the prefix can usefully carry: the line degrades to the
        # prefix alone, itself clipped, rather than folding onto a second row.
        return _clip_columns(prefix, width), ""
    if keep_tail:
        return prefix, ELLIPSIS + _clip_columns_tail(value, budget - marker)
    return prefix, _clip_columns(value, budget - marker) + ELLIPSIS


class StreamRunConsole:
    """Renders the Run console onto one stream — stderr in production — and, when
    the operator opts in, the Backend feed onto a second one — stdout in production
    (register G11). Only ``cli`` constructs it, and the two rendering choices an
    operator makes are its two keyword arguments: *feed* off is the default view and
    on restores the Backend's running commentary with speaker prefixes, and *quiet*
    drops the status line and the Iteration blocks while keeping the header, the
    summary, and every failure. Whether the result is painted for a terminal or
    plain is not chosen here at all — it is read from the stream at emit time."""

    def __init__(
        self, stream: TextIO, *, feed: TextIO | None = None, quiet: bool = False
    ) -> None:
        self._stream = stream
        # The opt-in Backend feed's stream, or ``None`` when the commentary stays
        # suppressed (register G2). It is a *second* stream on purpose: the operator
        # can redirect the transcript to a file and keep watching the dashboard.
        self._feed = feed
        self._quiet = quiet
        # Per-speaker partial feed lines. A Backend that streams deltas hands over
        # fragments of a line, and a prefix belongs at the start of a line, not in
        # the middle of one -- so the fragments are held here until the line ends.
        self._feed_pending: dict[str | None, str] = {}
        # What the feed calls the Backend, learned from the run header.
        self._backend = UNNAMED_BACKEND
        # All writes -- the main thread's operator lines and the ticker's repaints --
        # go through one re-entrant lock so a repaint never interleaves with a line.
        self._lock = threading.RLock()
        # The status line's live state, reset each Iteration. ``_active`` spans an
        # Iteration; ``_established`` turns on when the Iteration opens, so its clock
        # runs whether or not the Backend ever reports anything; ``_painted`` records
        # whether an in-place status currently sits at the cursor.
        self._status_active = False
        self._status_established = False
        self._status_painted = False
        self._painted_width = 0
        self._iteration = 0
        self._iterations = 0
        self._tool: str | None = None
        # ``None`` until the Backend names a tool: the line is established when the
        # Iteration opens, and a count no Backend has reported yet is absent rather
        # than a ``0 tools`` that reads as a fact (register G4/G5).
        self._tool_count: int | None = None
        # The Stage the Backend last declared and when, so the status line can stop
        # asserting it once it goes stale and fall back to the tool (register G6).
        self._stage: str | None = None
        self._stage_at: float | None = None
        self._context: int | None = None
        # ``None`` until a Backend supplies a subagent roster: a Backend that never
        # reports one (OpenCode) leaves the field absent rather than showing a zero
        # that reads as a fact (register G4/G5). A reported empty roster is a genuine
        # count of zero and does render.
        self._subagents: int | None = None
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
            if isinstance(observation, StageObserved):
                self._stage = observation.label
                self._stage_at = time.monotonic()
                self._establish_locked()
            elif isinstance(observation, ToolObserved):
                self._tool = observation.name
                self._tool_count = 1 if self._tool_count is None else self._tool_count + 1
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
                    f"iteration: {summarize_message(redact(neutralise(observation.quote)))}",
                )
            elif isinstance(observation, UnmarkedQuestion):
                self._message(
                    "warning",
                    "warning: final message ended on an unmarked operator-directed "
                    "question; continuing to the next iteration (no "
                    "<promise>NEEDS_INPUT</promise> marker and no question tool used): "
                    f"{summarize_message(redact(neutralise(observation.quote)))}",
                )
            elif isinstance(observation, (Narrated, ToolActivity, StepObserved)):
                self._commentary(observation)
            elif isinstance(observation, KilledTask):
                label = (
                    f"task {neutralise(observation.task_id)}"
                    if observation.task_id
                    else "an unnamed background task"
                )
                self._message(
                    "warning",
                    "warning: the backend killed a background task still running when "
                    "it ended its turn, so its work was left unverified: " + label,
                )

    def run_started(self, settings: RunSettings) -> None:
        # The feed names its speakers after the resolved Backend, so a captured
        # transcript says which one produced it without cross-referencing the header.
        self._backend = settings.backend
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

    def resume_started(self, settings: ResumeSettings) -> None:
        # Recovery's header, printed where its proof completes and immediately before
        # the process is replaced by the interactive session (register G8/G22). It is
        # deliberately three facts rather than the run's full header: everything else a
        # run states is about budget it is going to spend, and recovery spends none --
        # the operator is about to be sitting in the session themselves.
        self._fact(
            "resuming",
            f"{settings.backend} session {settings.session_id}, model {settings.model}",
        )
        self._fact("trust boundary", "re-proven: " + ", ".join(settings.reproven))
        # Stated either way. A relaxed guarantee already shouted itself through
        # ``deviation`` just above, but the header must not report host isolation only
        # by leaving it out, because an operator reading three lines cannot tell an
        # omission from a guarantee they still have.
        self._fact(
            "host isolation",
            "re-established; the resumed session is confined by the Seatbelt sandbox"
            if settings.host_isolated
            else "not enforced; the resumed session runs unconfined",
        )

    @staticmethod
    def _retained(runs: int) -> str:
        """How much evidence is at stake, worded in one place so the confirmation and
        the report that follows it can never name different numbers: both are handed
        the same ``CleanOutcome`` and reach the same phrase from it."""
        return f"{runs} run(s) of retained evidence"

    def confirm_removal(self, outcome: CleanOutcome) -> None:
        """What ``clean`` is about to destroy, put to the operator before any of it
        goes (register G22, decision J4).

        It states the same two facts the report states afterwards, off the same
        ``CleanOutcome``, so the number an operator agrees to is the number that is
        destroyed. The console asks; reading the answer belongs to ``cli`` -- this
        module writes to the operator, it does not interview them."""
        self._fact(
            "about to remove",
            self._retained(outcome.runs) if outcome.runs else "Ralph state; it holds no runs",
        )
        self._fact("state directory", str(outcome.state_root), keep_tail=True)
        # Unfitted and in the warning role: the question an irreversible destruction
        # turns on may be wrapped by a narrow window, never clipped short of the
        # answers it names (register G3/G19).
        self._message("warning", "this cannot be undone; remove it? [y/N]")

    def removal_declined(self, outcome: CleanOutcome) -> None:
        """The operator was asked and said no. It opens on ``removed`` like every
        other outcome of ``clean``, so one habit reads them all, and leads with
        ``nothing`` -- then names the answer that produced it, which is what tells it
        apart from having found nothing there to begin with."""
        self._fact("removed", "nothing; you declined the removal")
        self._fact("state directory", str(outcome.state_root), keep_tail=True)

    def state_removed(self, outcome: CleanOutcome) -> None:
        # ``clean`` destroys every run's evidence, so it says what it destroyed
        # (register G22). All three wordings open on ``removed`` so one habit reads
        # them all, and each puts what distinguishes it -- the count, or the word
        # ``nothing`` -- in its first few characters, ahead of the words a narrow
        # window spends. The path follows on its own fitted line, so shortening it can
        # never cost the operator the answer to "was there anything there?".
        if outcome.runs is None:
            self._fact("removed", "nothing; there was no Ralph state for this worktree")
        elif outcome.runs:
            self._fact("removed", self._retained(outcome.runs))
        else:
            self._fact("removed", "Ralph state; it held no runs")
        self._fact("state directory", str(outcome.state_root), keep_tail=True)

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
        # elapsed clock. The line is established here rather than by the first
        # Observation, so the clock ticks from zero for an Iteration a Backend
        # reports nothing about (register G3, US7); only its fields wait for facts.
        self._begin_iteration(number, iterations)
        if self._quiet:
            # Quiet drops the Iteration blocks and the status line, and nothing else:
            # the header, the summary, the warnings, and every failure still print,
            # so an unattended run stays quiet without losing its help (register G11).
            return
        # A rule opening the Iteration (register G2/G9): a full-width divider on a
        # terminal, embedding the number and the budget; a plain labelled line when
        # the stream has no width, so a piped log stays readable without the fill.
        width = self._width()
        if width is None:
            self._message("chrome", f"iteration {number} of {iterations}")
        else:
            opener = f"{PREFIX}── iteration {number} of {iterations} "
            # Fill to the window on a terminal, but never past it: a rule wider than
            # the window is clipped rather than folded onto a second row (register G3).
            fill = width - display_width(opener)
            line = _clip_columns(opener, width) if fill < 0 else opener + "─" * fill
            self._paint("", "chrome", redact(line), "chrome")
        # Established after the rule, so the first paint lands under the Iteration it
        # belongs to rather than being erased and redrawn around it. On both paths,
        # because off a terminal the same lifecycle drives the append-only heartbeat,
        # and that log's only liveness signal is no less owed (register G3/G11).
        with self._lock:
            self._establish_locked()

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
        if self._quiet:
            return
        session = neutralise(outcome.session_id) if outcome.session_id else "no session id"
        self._fact(
            f"iteration {outcome.number} of {outcome.iterations}",
            f"{outcome.outcome} in {format_duration(outcome.duration_seconds)}, "
            f"session {session}",
        )
        # The Backend's concluding message: the one utterance worth keeping, shown as
        # Backend content (uncoloured) and truncated for display only. Neutralised and
        # then redacted before it is summarized (register J2), so the placeholder is
        # what the display limit measures and a secret can never be clipped back into
        # view -- the same order the two migrated warnings above use.
        message = summarize_message(redact(neutralise(outcome.concluding_message or "")))
        if message.strip():
            self._content(message)

    def run_finished(self, summary: RunSummary) -> None:
        # A terminal path may arrive without an ``iteration_finished`` (a failure mid
        # session), so stop the ticker and erase any painted status here too before
        # the summary prints.
        self._finalize()
        self._ring_bell()
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
        self._line("failure", f"reason: {neutralise(help.reason)}")
        self._line("failure", f"ralph run: {help.run_id}")
        if help.session_id:
            self._line("failure", f"{help.backend} session: {neutralise(help.session_id)}")
        if help.detail:
            self._line("failure", f"question/error: {neutralise(help.detail)}")
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

    def relaunching_full_auto(self) -> None:
        # ``resume`` hands control to an interactive session started in the same
        # dangerous full-auto mode a run uses. A run states that caveat as a header
        # setting (register G7); recovery has no header yet, so it stays the loud
        # line it has always been -- worded here, like every other operator-facing
        # string, so no module outside the Run console addresses a terminal
        # (register G13/G14). Emitted whole through ``_line``: its ``WARNING:``
        # opener is the first-column anchor, not the header's ``ralph: `` voice.
        self._line(
            "warning",
            "WARNING: Ralph is relaunching the backend session in dangerous full-auto "
            "mode; it may edit files and run commands without confirmation.",
        )

    def failure_help(self, reason: str, run_dir: Path) -> None:
        # A failure that left a run directory behind is told what failed and where the
        # evidence is, not just one sentence (register G10). The reason keeps the
        # failure role and the ``ralph: `` voice the one-line handler used, so the
        # existing stderr assertions on it still hold; the next step names the run
        # directory itself — the retained diagnostics, and the backend's stream when a
        # session got that far — so the block carries the evidence path on its own.
        self._message("failure", neutralise(reason))
        self._message(
            "chrome",
            f"next step: inspect the retained diagnostics under {run_dir}, then run "
            "Ralph again once the cause is resolved",
        )

    def failed(self, message: str) -> None:
        """The one-line failure: an argument or precondition that stopped the run
        before a run directory existed, so there is no evidence path to point at and
        no help block to print (register G10).

        It rings the bell like any other terminal outcome (register G12). The
        counter-argument -- nobody walks away from a run that failed at invocation --
        does not cover the path: the same handler carries a ``RalphError`` raised from
        the git context, the prompt, the worktree lock, and a failed ``resume``
        handover, and an operator can be away for any of those."""
        self._finalize()
        self._ring_bell()
        self._message("failure", message)

    def _ring_bell(self) -> None:
        # On a terminal only, so an operator who walked away is called back on every
        # terminal outcome and a piped log carries no control character (register
        # G12). It occupies no column, so no width measurement counts it.
        if not self._is_terminal():
            return
        with self._lock:
            self._stream.write(BELL)
            self._stream.flush()

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

    def _speaker(self, subagent: str | None) -> str:
        """Who a feed line is attributed to: the Backend, or the specific subagent
        that produced it (register G11)."""
        who = self._backend
        if subagent:
            who = f"{who}{FEED_SUBAGENT_SEPARATOR}{neutralise(subagent)}"
        return who + FEED_SPEAKER_DELIMITER

    def _commentary(self, observation: Narrated | ToolActivity | StepObserved) -> None:
        """The Backend's running commentary: the opt-in feed's business and nothing
        else, so with no feed stream it is dropped here and never reaches the
        dashboard (register G2). This is the one place that decision is taken."""
        if self._feed is None:
            return
        if isinstance(observation, Narrated):
            self._feed_narrated(observation)
        elif isinstance(observation, ToolActivity):
            state = f" ({observation.state})" if observation.state else ""
            self._feed_marker(f"{observation.name}{state}", observation.subagent)
        else:
            self._feed_marker(
                "step started" if observation.started else "step finished", None
            )

    def _feed_narrated(self, observation: Narrated) -> None:
        """Append a passage of narration to its speaker's line and emit whatever
        that completes. A whole message ends its line; a streamed fragment leaves it
        open, so a prefix only ever lands at the start of a line and a sentence split
        across deltas is not split across rows."""
        pending = self._feed_pending.get(observation.subagent, "") + observation.text
        if not observation.partial:
            pending += "\n"
        lines = pending.split("\n")
        self._feed_pending[observation.subagent] = lines.pop()
        for line in lines:
            self._feed_write(observation.subagent, line)

    def _feed_marker(self, label: str, subagent: str | None) -> None:
        """A bracketed progress marker on the feed. Its speaker's open line is closed
        first, so the marker starts a row of its own rather than landing mid-sentence."""
        self._feed_flush_speaker(subagent)
        self._feed_write(subagent, f"[{label}]")

    def _feed_flush_speaker(self, subagent: str | None) -> None:
        pending = self._feed_pending.pop(subagent, "")
        if pending:
            self._feed_write(subagent, pending)

    def _feed_flush_locked(self) -> None:
        # An Iteration can end with a message that never reached a line break; the
        # operator is owed the last of it, so every open line is closed here.
        if self._feed is None:
            return
        for subagent in list(self._feed_pending):
            self._feed_flush_speaker(subagent)

    def _feed_write(self, subagent: str | None, text: str) -> None:
        # Ralph paints nothing onto the feed, on a terminal or off it (register G12):
        # the speaker prefix is plain text and no palette role is applied, so a
        # redirected transcript carries no ANSI of Ralph's own and nothing here can be
        # read as Ralph's voice. Backend text keeps its own colour and indentation and
        # loses what would let it leave its row (``neutralise_feed``), and goes through
        # the same redaction choke point as every other operator-facing string
        # (register G17). The speaker is neutralised too: a subagent identifier is
        # Backend-authored, and a newline in one would put the prefix mid-line.
        if self._feed is None:
            return
        with self._lock:
            # The status line is erased and redrawn around a feed line exactly as it
            # is around an operator line: an un-redirected ``--verbose`` puts both
            # streams on one terminal, where a feed line would otherwise land on top
            # of the painted status (register G3). Both calls are no-ops when nothing
            # is painted, so a redirected feed costs the dashboard nothing.
            self._erase_status_locked()
            # Redaction is a standing guarantee (register G17); the Backend's colour
            # is a convenience. Keeping SGR means a credential spliced with one is not
            # rejoined, so ``redact`` cannot match it -- and the retained-artifact
            # matcher cannot either, because the surviving parameter bytes are
            # printable, not control codepoints. A line whose secret only appears once
            # the escapes go is therefore rendered without them.
            body = neutralise_feed(text)
            bare = neutralise(text)
            if redact(bare) != bare:
                body = bare
            self._feed.write(redact(self._speaker(subagent) + body) + "\n")
            self._feed.flush()
            self._paint_status_locked()

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
            self._tool_count = None
            self._stage = None
            self._stage_at = None
            self._context = None
            self._subagents = None
            self._iteration_start = time.monotonic()

    def _establish_locked(self) -> None:
        # Opening the Iteration turns the status line on and starts the ticker that
        # keeps its clock moving; every Observation after that only repaints it. The
        # line is established before any Observation on purpose: an Iteration a
        # Backend reports nothing about otherwise had no clock and no heartbeat for
        # its whole duration -- no liveness signal at all, which is a third state
        # beside running and stalled that US7 does not account for. What waits for
        # facts is the fields, not the clock (register G4/G5).
        # Quiet establishes nothing: with no status line there is no clock to keep
        # moving and no heartbeat to append, so the ticker never starts (register G11).
        if self._quiet:
            return
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
            self._feed_flush_locked()
            self._erase_status_locked()
            stop, thread = self._ticker_stop, self._ticker_thread
            self._ticker_stop = self._ticker_thread = None
        if stop is not None:
            stop.set()
        if thread is not None:
            # Bounded, and safe to give up on: clearing ``_ticker_stop`` above is what
            # a ticker still in flight reads as "you have been superseded", so one
            # that outlives this wait retires instead of repainting alongside the next
            # Iteration's own ticker.
            thread.join(timeout=TICKER_JOIN_SECONDS)

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
                if not self._tick(stop):
                    return
            except (ValueError, OSError):
                # The stream was closed under us; stop rather than raising on a
                # daemon thread the run no longer depends on.
                return

    def _tick(self, stop: threading.Event) -> bool:
        """One repaint or heartbeat, and whether this ticker should keep going.

        *stop* is the ticker's own identity: ``_finalize``'s bounded wait may give up
        on a ticker that is still in flight, and the next Iteration then starts one of
        its own. A ticker whose event the console no longer holds is that abandoned
        one, so it paints nothing -- and is told to retire rather than left to notice
        at its next wake, so the two never overlap even for one tick."""
        with self._lock:
            if self._ticker_stop is not stop:
                return False
            if not (self._status_active and self._status_established):
                return True
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
                    self._stream.write(self._status_text() + "\n")
                    self._stream.flush()
        return True

    def _status_text(self) -> str:
        """The status line, already through the redaction choke point (register G17).
        The two free-text fields -- the Stage the Backend declared and the tool it
        named -- are redacted *before* rendering rather than after, so a ``[redacted]``
        placeholder is what the no-wrap fitting measures and a secret can never be
        clipped back into view by the width logic. Both painters read this one text, so
        neither the terminal repaint nor the off-terminal heartbeat can bypass it."""
        elapsed = (
            time.monotonic() - self._iteration_start
            if self._iteration_start is not None
            else 0.0
        )
        return render_status(
            self._iteration,
            self._iterations,
            elapsed,
            redact(neutralise(self._tool)) if self._tool else None,
            self._tool_count,
            self._context,
            self._subagents,
            self._width(),
            stage=redact(neutralise(self._stage)) if self._stage else None,
            stage_age_seconds=(
                time.monotonic() - self._stage_at if self._stage_at is not None else None
            ),
        )

    def _paint_status_locked(self) -> None:
        # Repaint the in-place status line, but only on a terminal, only while the
        # Iteration is live, and only once an Observation has established it. Uses a
        # carriage return, never a cursor-control escape, so ``NO_COLOR`` on a terminal
        # still emits no escape; colour is added only when colour is enabled.
        if not (self._status_active and self._status_established and self._is_terminal()):
            return
        text = self._status_text()
        # Columns, not codepoints: the erase overwrites this many spaces, so a wide
        # character measured as one column would leave the tail of the line on the
        # operator's screen under whatever printed next.
        self._painted_width = display_width(text)
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
