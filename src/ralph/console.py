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


class RunConsole(Protocol):
    """The operator-facing seam. It widens as later tickets migrate their emit
    sites; it is structural, matched with no runtime class or ABC machinery, in the
    style of the Backend Protocol."""

    def run_started(self, settings: RunSettings) -> None: ...

    def interactive_only_resolved(self, label: str, issues: list[int]) -> None: ...

    def failed(self, message: str) -> None: ...


def format_timeout(timeout: float) -> str:
    # `--timeout 0` deliberately disables the limit, which is a different fact
    # from "zero seconds" and must not be shown as one.
    return "disabled" if timeout <= 0 else f"{timeout:g}s"


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

    def _message(self, role: str, text: str) -> None:
        """A warning or a failure, never fitted. Their wording is what the operator
        greps for and what register G19 pins byte-identical, so a narrow window may
        wrap one; it may not clip words out of it. Only the header trades
        characters for a window (register G3), and only because every fact it drops
        is recoverable from the run directory it names."""
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
