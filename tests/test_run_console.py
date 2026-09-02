"""The Run console: the run header an operator is shown before budget is spent,
the terminal and piped rendering paths, and the structural rule that no other
module addresses a terminal.

Two tiers, as the program's testing decisions require. The black-box CLI seam
asserts what a real run tells an operator; the unit tests below drive the console
directly against a real pseudo-terminal of a known width, because width
truncation and colour suppression are miserable to prove through a subprocess."""

from __future__ import annotations

import ast
import io
import json
import os
from pathlib import Path
import re
import time
import unittest
from unittest import mock

from harness import ROOT, PtyCapture, RalphCliTestCase
from ralph.console import (
    CLAUDE_AGENTS_DEVIATION,
    NO_SANDBOX_DEVIATION,
    OPENCODE_AGENTS_DEVIATION,
    STAGE_STALE_SECONDS,
    CleanOutcome,
    ContextObserved,
    Deviation,
    IterationOutcome,
    KilledTask,
    MarkerWithdrawn,
    Narrated,
    OperatorHelp,
    ResumeSettings,
    RunSettings,
    RunSummary,
    StageObserved,
    StepObserved,
    StreamRunConsole,
    SubagentsObserved,
    ToolActivity,
    ToolObserved,
    UnmarkedQuestion,
    render_status,
)


def without_ansi(text: str) -> str:
    """What the same output would have been on a stream that is not a terminal --
    the only form worth asserting on, since colour is a rendering detail and the
    facts are not. The bell is dropped too: it is a terminal signal that occupies no
    column, so a width measurement must not count it."""
    while "\033[" in text:
        start = text.index("\033[")
        text = text[:start] + text[text.index("m", start) + 1 :]
    return text.replace("\a", "")


def _settings(**overrides: object) -> RunSettings:
    defaults: dict[str, object] = {
        "backend": "opencode",
        "model": "openai/gpt-5.6-sol",
        "iterations": 4,
        "timeout": 3600.0,
        "repository": "example/project",
        "branch": "main",
        "worktree": Path("/Users/operator/code/project"),
        "prompt_path": Path("/Users/operator/code/project/prompt.md"),
        "interactive_label": "may-ask-owner",
        "run_dir": Path(
            "/Users/operator/code/project/.git/ralph/runs/20260810T101112.131415Z-0a1b2c3d"
        ),
        "dirty": False,
    }
    defaults.update(overrides)
    return RunSettings(**defaults)  # type: ignore[arg-type]


class TerminalConsoleTest(unittest.TestCase):
    """Drives the console onto a real pty of a known width, and asserts what lands
    there -- never how it was painted."""

    def _shown(self, columns: int, say: object) -> list[str]:
        with PtyCapture(columns) as terminal:
            stream = terminal.text_stream()
            try:
                say(StreamRunConsole(stream))
            finally:
                stream.close()
        return terminal.text.rstrip("\n").split("\n")

    def _render(self, columns: int = 100, **overrides: object) -> list[str]:
        settings = _settings(**overrides)
        return self._shown(columns, lambda console: console.run_started(settings))

    def _plain(self, lines: list[str]) -> list[str]:
        return [without_ansi(line) for line in lines]

    def test_a_terminal_gets_colour_and_a_pipe_gets_none(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NO_COLOR", None)
            terminal = self._render()
        self.assertTrue(
            any("\033[" in line for line in terminal),
            f"expected the palette on a terminal, got {terminal}",
        )
        # The same facts, unchanged, once the escapes are removed.
        self.assertIn(
            "ralph: backend opencode, model openai/gpt-5.6-sol", self._plain(terminal)
        )

    def test_no_color_suppresses_the_palette_on_a_terminal(self) -> None:
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            lines = self._render()
        self.assertNotIn("\033[", "".join(lines))
        self.assertIn("ralph: backend opencode, model openai/gpt-5.6-sol", lines)

    def test_a_narrow_terminal_shortens_fields_instead_of_wrapping(self) -> None:
        columns = 48
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            lines = self._render(columns=columns)
        for line in lines:
            self.assertLessEqual(len(line), columns, f"{line!r} would wrap at {columns}")
        # A shortened path keeps its informative end: the run id stays readable
        # even when the leading directories do not fit.
        run_line = next(line for line in lines if line.startswith("ralph: run directory"))
        self.assertIn("0a1b2c3d", run_line)
        self.assertIn("...", run_line)
        # Shortening drops characters, never information the operator cannot infer:
        # every header fact is still present as its own line.
        self.assertEqual(len(lines), len(self._render(columns=200)))

    def test_a_dirty_worktree_is_warned_about_in_the_header(self) -> None:
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            clean = self._render()
            dirty = self._render(dirty=True)
        self.assertFalse(any("uncommitted changes" in line for line in clean))
        self.assertIn("ralph: warning: worktree has uncommitted changes", dirty)

    def test_a_disabled_timeout_is_stated_as_disabled_not_as_zero(self) -> None:
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            lines = self._render(timeout=0.0)
        self.assertIn("ralph: iterations 4, timeout disabled", lines)

    def test_a_failure_survives_a_narrow_terminal_word_for_word(self) -> None:
        # A header fact can afford to lose characters -- the run directory it names
        # holds the whole of it. A failure cannot: its wording is what an operator
        # greps for, and one of these texts exists nowhere else. It may wrap; it
        # may not be clipped.
        message = (
            "Claude customizations must be disabled before running Ralph; a "
            "repository agents directory is present, and --unsafe-allow-agents is "
            "the supported opt-out for that vector alone"
        )
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            shown = "\n".join(self._shown(48, lambda console: console.failed(message)))
        self.assertIn(message, shown.replace("\n", ""))

    def test_operator_facing_output_is_redacted_at_the_console(self) -> None:
        from ralph.redaction import set_active_redactor

        secret = "s3cr3t-subscription-token-value"
        set_active_redactor([secret])
        self.addCleanup(set_active_redactor, [])
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            lines = self._render(model=f"model-{secret}")
        self.assertNotIn(secret, "\n".join(lines))
        self.assertIn("[redacted]", "\n".join(lines))

    def test_a_terminal_outcome_rings_the_bell(self) -> None:
        summary = RunSummary(
            outcome="complete",
            run_dir=Path("/w/.git/ralph/runs/20260810T101112.131415Z-0a1b2c3d"),
            initial_branch="main",
            final_branch="main",
            dirty=False,
            upstream=None,
            ahead=0,
        )
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            shown = self._shown(80, lambda console: console.run_finished(summary))
        self.assertIn("\a", "".join(shown))

    def test_the_status_line_repaints_in_place_carrying_the_progress_fields(self) -> None:
        # The whole point of the program on a real terminal: progress Observations
        # paint a status line in place, and each visual row (the pty overwrites with
        # carriage returns) fits the window and never wraps (register G3/G4).
        columns = 80

        def drive(console: StreamRunConsole) -> None:
            console.iteration_started(1, 4)
            console.observe(ToolObserved("Bash"))
            console.observe(SubagentsObserved(("survey", "review")))
            console.observe(ContextObserved(12483))
            console.iteration_finished(
                IterationOutcome(1, 4, 5.0, "complete", "ses_1", "Done.")
            )

        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            shown = "\n".join(self._shown(columns, drive))
        self.assertIn("Bash", shown)
        self.assertIn("2 subagents", shown)
        self.assertIn("context 12483 tokens", shown)
        for row in re.split(r"[\r\n]", shown):
            self.assertLessEqual(len(without_ansi(row)), columns, f"{row!r} would wrap")
        # The status line was erased before the outcome block, which stands clean.
        self.assertIn("ralph: iteration 1 of 4 complete", shown)

    def test_the_iteration_and_summary_lines_never_wrap_a_narrow_terminal(self) -> None:
        columns = 44
        outcome = IterationOutcome(
            number=1,
            iterations=4,
            duration_seconds=63.0,
            outcome="budget_exhausted",
            session_id="ses_1",
            concluding_message="Implemented and verified the change across every module.",
        )
        summary = RunSummary(
            outcome="budget_exhausted",
            run_dir=Path("/Users/operator/code/project/.git/ralph/runs/20260810T101112.131415Z-0a1b2c3d"),
            initial_branch="main",
            final_branch="agent-branch",
            dirty=True,
            upstream="origin/main",
            ahead=3,
        )
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            shown = self._shown(
                columns,
                lambda console: (
                    console.iteration_started(1, 4),
                    console.trust_boundary_established(
                        ["subscription-only authentication", "customization isolation", "host isolation"]
                    ),
                    console.iteration_finished(outcome),
                    console.run_finished(summary),
                ),
            )
        for line in shown:
            self.assertLessEqual(
                len(without_ansi(line)), columns, f"{line!r} would wrap at {columns} columns"
            )


class PipedConsoleTest(unittest.TestCase):
    """Drives the console onto a plain stream that is not a terminal -- the piped
    path an operator captures to a file. It exercises the Iteration outcome blocks
    and the terminal summary directly, where proving the exact wording, the
    bell-versus-no-bell distinction, and display-only truncation through a subprocess
    would be miserable."""

    def _lines(self, say: object) -> list[str]:
        stream = io.StringIO()
        say(StreamRunConsole(stream))
        return stream.getvalue().rstrip("\n").split("\n")

    def _summary(self, **overrides: object) -> RunSummary:
        defaults: dict[str, object] = {
            "outcome": "complete",
            "run_dir": Path(
                "/w/.git/ralph/runs/20260810T101112.131415Z-0a1b2c3d"
            ),
            "initial_branch": "main",
            "final_branch": "main",
            "dirty": False,
            "upstream": None,
            "ahead": 0,
        }
        defaults.update(overrides)
        return RunSummary(**defaults)  # type: ignore[arg-type]

    def test_each_iteration_closes_with_its_outcome_and_concluding_message(self) -> None:
        lines = self._lines(
            lambda console: console.iteration_finished(
                IterationOutcome(
                    number=2,
                    iterations=4,
                    duration_seconds=63.0,
                    outcome="budget_exhausted",
                    session_id="ses_7",
                    concluding_message="Implemented and verified.",
                )
            )
        )
        joined = "\n".join(lines)
        # The block names the Iteration, its outcome, its duration, and the session
        # to resume, then quotes the Backend's concluding message.
        self.assertIn("iteration 2 of 4", joined)
        self.assertIn("budget_exhausted", joined)
        self.assertIn("1m03s", joined)
        self.assertIn("session ses_7", joined)
        self.assertIn("Implemented and verified.", joined)

    def test_an_iteration_without_a_session_id_says_so(self) -> None:
        lines = self._lines(
            lambda console: console.iteration_finished(
                IterationOutcome(1, 1, 4.0, "complete", None, "Done.")
            )
        )
        self.assertIn("session no session id", "\n".join(lines))

    def test_a_run_ends_with_a_summary_naming_the_git_outcome_and_evidence(self) -> None:
        lines = self._lines(lambda console: console.run_finished(self._summary()))
        self.assertIn("ralph: outcome run complete", lines)
        self.assertIn("ralph: branch main", lines)
        self.assertIn("ralph: worktree clean", lines)
        self.assertIn(
            "ralph: evidence /w/.git/ralph/runs/20260810T101112.131415Z-0a1b2c3d",
            lines,
        )

    def test_budget_exhaustion_keeps_the_greppable_phrase(self) -> None:
        lines = self._lines(
            lambda console: console.run_finished(self._summary(outcome="budget_exhausted"))
        )
        self.assertIn("iteration budget exhausted", "\n".join(lines))

    def test_a_piped_terminal_outcome_emits_no_bell(self) -> None:
        stream = io.StringIO()
        StreamRunConsole(stream).run_finished(self._summary())
        self.assertNotIn("\a", stream.getvalue())
        self.assertNotIn("\033", stream.getvalue())

    def test_a_branch_change_is_reported_in_the_summary(self) -> None:
        unchanged = self._lines(lambda console: console.run_finished(self._summary()))
        changed = self._lines(
            lambda console: console.run_finished(
                self._summary(final_branch="agent-branch")
            )
        )
        self.assertFalse(any("changed from" in line for line in unchanged))
        self.assertIn("ralph: branch changed from main to agent-branch", changed)

    def test_the_summary_states_whether_work_was_pushed(self) -> None:
        no_upstream = self._lines(lambda console: console.run_finished(self._summary()))
        self.assertTrue(any("nothing pushed" in line for line in no_upstream))

        ahead = self._lines(
            lambda console: console.run_finished(
                self._summary(upstream="origin/main", ahead=2)
            )
        )
        self.assertTrue(any("2 commit(s) not pushed to origin/main" in line for line in ahead))

        pushed = self._lines(
            lambda console: console.run_finished(
                self._summary(upstream="origin/main", ahead=0)
            )
        )
        self.assertTrue(any("pushed to origin/main" in line for line in pushed))

    def test_a_dirty_worktree_is_reported_in_the_summary(self) -> None:
        clean = self._lines(lambda console: console.run_finished(self._summary()))
        dirty = self._lines(
            lambda console: console.run_finished(self._summary(dirty=True))
        )
        self.assertIn("ralph: worktree clean", clean)
        self.assertTrue(any("dirty" in line for line in dirty))

    def test_the_concluding_message_is_truncated_for_display(self) -> None:
        message = "verified. " * 100  # far past the display limit
        lines = self._lines(
            lambda console: console.iteration_finished(
                IterationOutcome(1, 1, 1.0, "complete", "ses_1", message)
            )
        )
        content = next(line for line in lines if "verified." in line)
        self.assertTrue(content.endswith("..."))
        self.assertLess(len(content), len(message))

    def test_the_trust_boundary_names_only_the_proven_properties(self) -> None:
        full = self._lines(
            lambda console: console.trust_boundary_established(
                ["subscription-only authentication", "customization isolation", "host isolation"]
            )
        )
        self.assertIn(
            "ralph: trust boundary proven: subscription-only authentication, "
            "customization isolation, host isolation",
            full,
        )
        relaxed = self._lines(
            lambda console: console.trust_boundary_established(
                ["subscription-only authentication", "customization isolation"]
            )
        )
        self.assertFalse(any("host isolation" in line for line in relaxed))

    def test_the_iteration_events_carry_no_ansi_on_a_pipe(self) -> None:
        lines = self._lines(
            lambda console: (
                console.iteration_started(1, 2),
                console.iteration_finished(
                    IterationOutcome(1, 2, 1.0, "complete", "ses_1", "Done.")
                ),
            )
        )
        self.assertNotIn("\033", "\n".join(lines))


class HelpAndDeviationConsoleTest(unittest.TestCase):
    """Drives the full help block, the deviation warnings, and the continuation line
    onto a plain (non-terminal) stream, where their exact wording, first-column
    anchors, and redaction are what an operator and any tooling depend on. Colour and
    the bell are terminal concerns proven on a real pty above; here the facts stand
    alone."""

    def _lines(self, say: object) -> list[str]:
        stream = io.StringIO()
        say(StreamRunConsole(stream))
        return stream.getvalue().rstrip("\n").split("\n")

    def _handoff(self, **overrides: object) -> OperatorHelp:
        defaults: dict[str, object] = {
            "reason": "OpenCode requested operator input",
            "run_id": "20260810T101112.131415Z-0a1b2c3d",
            "remaining": 2,
            "backend": "opencode",
            "session_id": "ses_1",
            "detail": "Should I preserve the legacy file?",
            "resume_command": "cd /w && ralph resume --backend opencode --session ses_1",
            "continue_command": "cd /w && ralph run prompt.md --backend opencode --iterations 2",
        }
        defaults.update(overrides)
        return OperatorHelp(**defaults)  # type: ignore[arg-type]

    def test_the_handoff_block_names_the_reason_session_and_recovery_commands(self) -> None:
        lines = self._lines(lambda console: console.operator_help(self._handoff()))
        joined = "\n".join(lines)
        self.assertIn("========== RALPH NEEDS OPERATOR ==========", lines)
        self.assertIn("reason: OpenCode requested operator input", lines)
        self.assertIn("ralph run: 20260810T101112.131415Z-0a1b2c3d", lines)
        self.assertIn("opencode session: ses_1", lines)
        self.assertIn("question/error: Should I preserve the legacy file?", lines)
        self.assertIn("iterations remaining: 2", lines)
        # The recovery lines keep their first-column anchors: tooling and habits match
        # ``manual resume: `` and ``continue Ralph: `` from the start of the line, so
        # they must not gain the header's ``ralph: `` prefix.
        self.assertTrue(any(line.startswith("manual resume: ") for line in lines), joined)
        self.assertTrue(any(line.startswith("continue Ralph: ") for line in lines), joined)

    def test_the_handoff_block_without_a_session_omits_resume_but_keeps_the_restart(self) -> None:
        lines = self._lines(
            lambda console: console.operator_help(
                self._handoff(session_id=None, detail=None, resume_command=None)
            )
        )
        self.assertIn("RALPH NEEDS OPERATOR", "\n".join(lines))
        self.assertFalse(any(line.startswith("manual resume: ") for line in lines))
        self.assertFalse(any("session:" in line for line in lines))
        self.assertTrue(any(line.startswith("continue Ralph: ") for line in lines))
        self.assertIn("iterations remaining: 2", lines)

    def test_the_handoff_block_with_no_budget_left_says_so(self) -> None:
        lines = self._lines(
            lambda console: console.operator_help(
                self._handoff(remaining=0, continue_command=None)
            )
        )
        self.assertIn("iterations remaining: 0", lines)
        self.assertIn("No iterations remain to continue Ralph.", lines)
        self.assertFalse(any(line.startswith("continue Ralph: ") for line in lines))

    def test_the_handoff_block_is_redacted_at_the_console(self) -> None:
        from ralph.redaction import set_active_redactor

        secret = "oauth-secret-value-987654321"
        set_active_redactor([secret])
        self.addCleanup(set_active_redactor, [])
        lines = self._lines(
            lambda console: console.operator_help(
                self._handoff(reason=f"backend leaked {secret} mid-run")
            )
        )
        joined = "\n".join(lines)
        self.assertNotIn(secret, joined)
        self.assertIn("[redacted]", joined)

    def test_the_handoff_block_carries_no_ansi_or_bell_on_a_pipe(self) -> None:
        stream = io.StringIO()
        StreamRunConsole(stream).operator_help(self._handoff())
        self.assertNotIn("\033", stream.getvalue())
        self.assertNotIn("\a", stream.getvalue())

    def test_a_deviation_states_the_relaxed_guarantee_loudly(self) -> None:
        sandbox = self._lines(
            lambda console: console.deviation(Deviation(NO_SANDBOX_DEVIATION))
        )
        joined = "\n".join(sandbox)
        self.assertIn("--unsafe-no-sandbox is set", joined)
        self.assertIn("NOT proving host isolation", joined)
        claude = self._lines(
            lambda console: console.deviation(Deviation(CLAUDE_AGENTS_DEVIATION))
        )
        self.assertIn("--unsafe-allow-agents is set", "\n".join(claude))
        self.assertIn("Ralph is not proving Claude subagent isolation", "\n".join(claude))
        opencode = self._lines(
            lambda console: console.deviation(Deviation(OPENCODE_AGENTS_DEVIATION))
        )
        self.assertIn("Ralph is not proving OpenCode agent isolation", "\n".join(opencode))

    def test_a_deviation_carries_no_ansi_on_a_pipe(self) -> None:
        stream = io.StringIO()
        StreamRunConsole(stream).deviation(Deviation(NO_SANDBOX_DEVIATION))
        self.assertNotIn("\033", stream.getvalue())

    def test_budget_exhaustion_states_the_command_that_continues_the_work(self) -> None:
        command = "cd /w && ralph run prompt.md --backend opencode --iterations 4"
        lines = self._lines(lambda console: console.budget_continue(command))
        self.assertTrue(any(line.startswith("continue Ralph: ") for line in lines))
        self.assertIn(command, "\n".join(lines))

    def test_a_backend_failure_names_the_reason_and_a_next_step_not_the_banner(self) -> None:
        run_dir = Path("/w/.git/ralph/runs/20260810T101112.131415Z-0a1b2c3d")
        lines = self._lines(
            lambda console: console.failure_help("OpenCode session failed", run_dir)
        )
        joined = "\n".join(lines)
        # The reason keeps the one-line handler's ``ralph: `` voice, so the existing
        # stderr assertions on it still hold; a next step names the evidence path.
        self.assertIn("ralph: OpenCode session failed", lines)
        self.assertTrue(any("next step" in line for line in lines), joined)
        self.assertIn(str(run_dir), joined)
        # A pre-session failure is not a resumable handoff, so the banner never shows.
        self.assertNotIn("RALPH NEEDS OPERATOR", joined)


class CleanAndResumeConsoleTest(unittest.TestCase):
    """The two commands that are not ``run``, rendered directly (register G22).
    ``clean`` destroys every run's evidence irreversibly, so what it says afterwards
    is the operator's only record of it; ``resume`` says its piece and then replaces
    its own process, so there is no second chance to render. Both are driven onto a
    plain stream here, where the wording is what matters, and onto a narrow pty below,
    where the answer must survive the window."""

    def _lines(self, say: object) -> list[str]:
        stream = io.StringIO()
        say(StreamRunConsole(stream))
        return stream.getvalue().rstrip("\n").split("\n")

    def _resume(self, **overrides: object) -> ResumeSettings:
        defaults: dict[str, object] = {
            "backend": "claude",
            "model": "claude-opus-5",
            "session_id": "claude-session-1",
            "host_isolated": True,
            "reproven": ("subscription-only authentication", "customization isolation"),
        }
        defaults.update(overrides)
        return ResumeSettings(**defaults)  # type: ignore[arg-type]

    def test_clean_names_the_runs_it_destroyed_and_where_they_were(self) -> None:
        state_root = Path("/Users/operator/code/project/.git/ralph")
        lines = self._lines(
            lambda console: console.state_removed(
                CleanOutcome(state_root, runs=55)
            )
        )
        joined = "\n".join(lines)
        self.assertIn("55 run(s)", joined)
        self.assertIn(str(state_root), joined)

    def test_clean_distinguishes_a_no_op_from_a_removal(self) -> None:
        state_root = Path("/w/.git/ralph")
        removed = "\n".join(
            self._lines(
                lambda console: console.state_removed(
                    CleanOutcome(state_root, runs=3)
                )
            )
        )
        nothing = "\n".join(
            self._lines(
                lambda console: console.state_removed(
                    CleanOutcome(state_root, runs=None)
                )
            )
        )
        # "Removed fifty-five runs" and "there was nothing there" are the two answers
        # an operator needs told apart; neither may read as the other.
        self.assertNotEqual(removed, nothing)
        self.assertIn("nothing", nothing)
        self.assertNotIn("nothing", removed)

    def test_removing_state_that_held_no_runs_is_not_reported_as_a_no_op(self) -> None:
        # A state directory holding a lock file and no runs was still destroyed. It
        # must not borrow the wording of the case where there was nothing to destroy.
        state_root = Path("/w/.git/ralph")
        emptied = "\n".join(
            self._lines(
                lambda console: console.state_removed(
                    CleanOutcome(state_root, runs=0)
                )
            )
        )
        nothing = "\n".join(
            self._lines(
                lambda console: console.state_removed(
                    CleanOutcome(state_root, runs=None)
                )
            )
        )
        self.assertNotEqual(emptied, nothing)
        self.assertNotIn("nothing", emptied)

    def test_the_resume_header_names_the_session_and_what_was_reproven(self) -> None:
        joined = "\n".join(self._lines(lambda console: console.resume_started(self._resume())))
        self.assertIn("claude", joined)
        self.assertIn("claude-opus-5", joined)
        self.assertIn("claude-session-1", joined)
        self.assertIn("subscription-only authentication", joined)
        self.assertIn("customization isolation", joined)

    def test_the_resume_header_states_host_isolation_either_way(self) -> None:
        confined = "\n".join(
            self._lines(lambda console: console.resume_started(self._resume()))
        )
        unconfined = "\n".join(
            self._lines(
                lambda console: console.resume_started(self._resume(host_isolated=False))
            )
        )
        # Never reported by omission: an operator reading three lines cannot tell a
        # guarantee that was dropped from one that was simply not mentioned.
        self.assertIn("host isolation", confined)
        self.assertIn("host isolation", unconfined)
        self.assertNotEqual(confined, unconfined)

    def test_both_headers_are_redacted_at_the_console(self) -> None:
        from ralph.redaction import set_active_redactor

        secret = "s3cr3t-subscription-token-value"
        set_active_redactor([secret])
        self.addCleanup(set_active_redactor, [])
        joined = "\n".join(
            self._lines(
                lambda console: (
                    console.resume_started(self._resume(session_id=f"ses-{secret}")),
                    console.state_removed(
                        CleanOutcome(Path(f"/w/{secret}/.git/ralph"), runs=1)
                    ),
                )
            )
        )
        self.assertNotIn(secret, joined)
        self.assertIn("[redacted]", joined)

    def test_neither_header_wraps_a_narrow_terminal(self) -> None:
        columns = 44
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}), PtyCapture(columns) as terminal:
            stream = terminal.text_stream()
            try:
                console = StreamRunConsole(stream)
                console.resume_started(self._resume())
                console.state_removed(
                    CleanOutcome(Path("/Users/operator/code/project/.git/ralph"), runs=55)
                )
            finally:
                stream.close()
        lines = terminal.text.rstrip("\n").split("\n")
        for line in lines:
            self.assertLessEqual(
                len(without_ansi(line)), columns, f"{line!r} would wrap at {columns} columns"
            )
        # The path is what a narrow window spends; the answer to "what happened?"
        # survives whole on its own line.
        self.assertTrue(
            any(line.startswith("ralph: removed") and "55 run(s)" in line for line in lines),
            lines,
        )


class RunHeaderTest(RalphCliTestCase):
    def test_the_run_header_states_the_settings_and_the_evidence_path(self) -> None:
        result = self.run_ralph("--iterations", "3", "--timeout", "900")

        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir = next((self.repo.resolve() / ".git" / "ralph" / "runs").iterdir())
        for expected in (
            "ralph: backend opencode, model openai/gpt-5.6-sol",
            "ralph: iterations 3, timeout 900s",
            "ralph: repository example/project, branch main",
            f"ralph: worktree {self.repo.resolve()}",
            f"ralph: prompt {self.prompt.resolve()}",
            "ralph: interactive-only label may-ask-owner",
            f"ralph: run directory {run_dir}",
        ):
            self.assertIn(expected, result.stderr)

    def test_the_standing_caveats_are_settings_rather_than_warning_paragraphs(
        self,
    ) -> None:
        result = self.run_ralph()

        self.assertEqual(result.returncode, 0, result.stderr)
        # The facts survive -- the operator is still told what full-auto costs and
        # what the power assertion cannot do.
        self.assertIn("dangerous full-auto", result.stderr)
        self.assertIn("caffeinate", result.stderr)
        # ... but neither shouts, so the block where a rare real warning lands is
        # still worth reading.
        self.assertNotIn("WARNING: Ralph always uses", result.stderr)
        self.assertNotIn("WARNING: caffeinate", result.stderr)

    def test_the_header_names_the_resolved_interactive_only_children(self) -> None:
        result = self.run_ralph(env={"FAKE_GH_ISSUE_LIST": '[{"number":41},{"number":12}]'})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "ralph: interactive-only children #12, #41 (label may-ask-owner)",
            result.stderr,
        )

    def test_no_open_interactive_only_children_is_said_rather_than_left_blank(
        self,
    ) -> None:
        result = self.run_ralph()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "ralph: interactive-only children none open (label may-ask-owner)",
            result.stderr,
        )

    def test_a_piped_run_carries_no_ansi_at_all(self) -> None:
        result = self.run_ralph()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("\033", result.stderr)

    def test_a_run_on_a_terminal_is_coloured_and_fits_the_window(self) -> None:
        columns = 72
        result = self.run_ralph_pty(columns=columns)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("\033[", result.stderr)
        for line in result.stderr.split("\n"):
            self.assertLessEqual(
                len(without_ansi(line)), columns, f"{line!r} would wrap at {columns} columns"
            )

    def test_no_color_on_a_terminal_still_prints_the_header(self) -> None:
        result = self.run_ralph_pty(env={"NO_COLOR": "1"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("\033", result.stderr)
        self.assertIn("ralph: backend opencode, model openai/gpt-5.6-sol", result.stderr)


class BackendFeedRunTest(RalphCliTestCase):
    """What a real run puts on each stream: the dashboard on stderr, the Backend's
    commentary nowhere at all until ``--verbose`` puts it on stdout (register
    G2/G11)."""

    def test_the_default_view_carries_no_backend_commentary_on_stdout(self) -> None:
        result = self.run_ralph()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        # The one utterance worth keeping survives, in the Iteration's outcome block
        # on stderr, where the rest of Ralph's voice already is.
        self.assertIn("Work complete.", result.stderr)

    def test_verbose_restores_the_feed_on_stdout_with_its_speaker(self) -> None:
        progress = json.dumps(
            {
                "type": "tool_use",
                "sessionID": "ses_1",
                "part": {"type": "tool", "tool": "bash", "state": {"status": "completed"}},
            }
        )
        events = progress + "\n" + self._events(
            "Work complete.\n<promise>COMPLETE</promise>"
        )
        result = self.run_ralph(
            "--verbose",
            env={
                "FAKE_EVENTS": events,
                "FAKE_EXPORT": self._export("Work complete.\n<promise>COMPLETE</promise>"),
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("opencode: [bash (completed)]", result.stdout)
        self.assertIn("opencode: Work complete.", result.stdout)
        # The dashboard is untouched by the opt-in and stays on stderr.
        self.assertIn("ralph: backend opencode", result.stderr)
        self.assertNotIn("ralph: backend opencode", result.stdout)

    def test_a_claude_subagent_is_named_apart_from_the_backend_it_serves(self) -> None:
        events = self._claude_multiturn_events(
            [
                {
                    "text": "Work complete.\n<promise>COMPLETE</promise>",
                    "subagents": ["Survey finished."],
                }
            ]
        )
        result = self.run_ralph(
            "--verbose", backend="claude", env={"FAKE_CLAUDE_EVENTS": events}
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("claude: Work complete.", result.stdout)
        self.assertIn("Survey finished.", result.stdout)
        # The subagent's line is attributed to it, not to the Backend.
        self.assertNotIn("claude: Survey finished.", result.stdout)

    def test_the_feed_carries_no_ansi_when_redirected_off_the_terminal(self) -> None:
        # stderr on a real terminal, stdout on a pipe: the dashboard is coloured and
        # the captured transcript stays clean, which is the whole point of splitting
        # the two streams. Ralph adds nothing to the feed, so with a backend that
        # emits no escapes the redirected transcript carries none at all.
        result = self.run_ralph_pty("--verbose")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("opencode: Work complete.", result.stdout)
        self.assertNotIn("\033", result.stdout)
        self.assertIn("\033[", result.stderr)


class QuietRunTest(RalphCliTestCase):
    """``--quiet`` for an unattended run: no status line and no Iteration blocks, but
    never at the cost of the header, the summary, or the help (register G11)."""

    def test_quiet_drops_the_iteration_blocks_but_keeps_header_and_summary(self) -> None:
        result = self.run_ralph("--quiet")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("iteration 1 of 1", result.stderr)
        self.assertNotIn("Work complete.", result.stderr)
        self.assertIn("ralph: backend opencode", result.stderr)
        self.assertIn("ralph: run directory", result.stderr)
        self.assertIn("ralph: outcome run complete", result.stderr)

    def test_quiet_never_costs_the_operator_their_help(self) -> None:
        # A run that spends the budget without completing still gets its summary and
        # the exact command that continues the work (register G10).
        unfinished = "Still working on it."
        result = self.run_ralph(
            "--quiet",
            env={
                "FAKE_EVENTS": self._events(unfinished),
                "FAKE_EXPORT": self._export(unfinished),
            },
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("iteration budget exhausted", result.stderr)
        self.assertIn("continue Ralph:", result.stderr)


class StatusLineRenderTest(unittest.TestCase):
    """``render_status`` is a pure function, so its no-wrap and drop-right-to-left
    behaviour is proven directly rather than through a terminal."""

    def test_every_field_is_present_left_to_right_when_it_fits(self) -> None:
        line = render_status(2, 4, 63.0, "Bash", 5, 12483, 3, 200)
        self.assertEqual(
            line,
            "ralph: iteration 2/4 · 1m03s · Bash · 5 tools · context 12483 tokens · "
            "3 subagents",
        )

    def test_fields_drop_right_to_left_and_the_line_never_exceeds_the_width(self) -> None:
        for width in range(8, 100):
            line = render_status(1, 4, 5.0, "Read", 2, 900, 1, width)
            self.assertLessEqual(len(line), width, f"width {width}: {line!r}")
        # A moderately narrow window keeps the Iteration and elapsed and drops the
        # tail from the right: the subagent count goes before the context gauge.
        narrow = render_status(1, 4, 5.0, "Read", 2, 900, 1, 34)
        self.assertIn("iteration 1/4", narrow)
        self.assertNotIn("subagent", narrow)
        self.assertNotIn("context", narrow)

    def test_a_stream_with_no_width_keeps_every_field(self) -> None:
        line = render_status(1, 1, 0.0, "Edit", 1, 5, 0, None)
        self.assertIn("context 5 tokens", line)
        self.assertIn("0 subagents", line)
        self.assertIn("1 tool", line)

    def test_an_unknown_tool_or_context_is_simply_omitted(self) -> None:
        line = render_status(1, 1, 0.0, None, 0, None, 0, None)
        self.assertEqual(line, "ralph: iteration 1/1 · 0s · 0 tools · 0 subagents")

    def test_an_absent_subagent_count_is_dropped_not_shown_as_zero(self) -> None:
        # A Backend that never reports a roster (OpenCode) leaves the count absent, so
        # the field is dropped entirely rather than inventing a "0 subagents" fact
        # (register G4/G5). A reported empty roster -- a genuine zero -- still renders,
        # so the two are never conflated.
        absent = render_status(1, 1, 0.0, "Bash", 2, 900, None, None)
        self.assertNotIn("subagent", absent)
        self.assertIn("2 tools", absent)
        self.assertIn("context 900 tokens", absent)
        zero = render_status(1, 1, 0.0, "Bash", 2, 900, 0, None)
        self.assertIn("0 subagents", zero)


class StatusLineStageTest(unittest.TestCase):
    """The declared Stage occupies the status line's one "what is it doing" field,
    and gives it back to the tool once it has gone stale (register G6)."""

    def test_a_declared_stage_is_shown_instead_of_the_tool(self) -> None:
        line = render_status(2, 4, 63.0, "Bash", 5, 12483, 3, 200, stage="implementing")
        self.assertIn("implementing", line)
        self.assertNotIn("Bash", line)
        # It takes the tool's slot rather than adding one, so the rest of the line
        # is unchanged and nothing is pushed off a narrow window by the addition.
        self.assertEqual(
            line,
            "ralph: iteration 2/4 · 1m03s · implementing · 5 tools · "
            "context 12483 tokens · 3 subagents",
        )

    def test_a_stale_stage_falls_back_to_the_last_tool(self) -> None:
        fresh = render_status(
            1, 2, 5.0, "Bash", 3, None, None, None, stage="loading context",
            stage_age_seconds=STAGE_STALE_SECONDS,
        )
        self.assertIn("loading context", fresh)
        stale = render_status(
            1, 2, 5.0, "Bash", 3, None, None, None, stage="loading context",
            stage_age_seconds=STAGE_STALE_SECONDS + 1,
        )
        self.assertNotIn("loading context", stale)
        self.assertIn("Bash", stale)

    def test_a_stale_stage_with_no_tool_yet_asserts_nothing(self) -> None:
        # Falling back to a tool that does not exist would be worse than saying
        # nothing: the field is simply absent rather than stating a stale phase.
        line = render_status(
            1, 2, 5.0, None, 0, None, None, None, stage="selecting",
            stage_age_seconds=STAGE_STALE_SECONDS + 1,
        )
        self.assertEqual(line, "ralph: iteration 1/2 · 5s · 0 tools")

    def test_a_stage_still_drops_before_the_iteration_and_elapsed(self) -> None:
        for width in range(8, 100):
            line = render_status(
                1, 4, 5.0, "Read", 2, 900, 1, width, stage="loading context"
            )
            self.assertLessEqual(len(line), width, f"width {width}: {line!r}")


class TerminalStatusStageTest(unittest.TestCase):
    """The Stage reaches a real status line the same way every other progress fact
    does -- as an Observation, with no adapter wording anything."""

    def test_a_secret_in_a_stage_label_never_reaches_the_terminal(self) -> None:
        # The Stage is the first Backend-authored free text to reach the status line,
        # and the line is painted rather than written -- so it has to pass the same
        # redaction choke point every other operator-facing string does (register G17).
        from ralph.redaction import set_active_redactor

        secret = "s3cr3t-subscription-token-value"
        set_active_redactor([secret])
        self.addCleanup(set_active_redactor, [])
        stream = _FakeTerminal()
        size = os.terminal_size((100, 24))
        with mock.patch("ralph.console.os.get_terminal_size", return_value=size), \
                mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=False):
            console = StreamRunConsole(stream)
            console.iteration_started(1, 2)
            console.observe(StageObserved(f"pushing {secret}"))
            console.iteration_finished(
                IterationOutcome(1, 2, 5.0, "complete", "ses_1", "Done.")
            )
        out = stream.getvalue()
        self.assertNotIn(secret, out)
        self.assertIn("[redacted]", out)

    def test_the_status_line_carries_the_stage_the_backend_declared(self) -> None:
        stream = _FakeTerminal()
        size = os.terminal_size((100, 24))
        with mock.patch("ralph.console.os.get_terminal_size", return_value=size), \
                mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=False):
            console = StreamRunConsole(stream)
            console.iteration_started(1, 2)
            console.observe(ToolObserved("Bash"))
            console.observe(StageObserved("implementing"))
            console.iteration_finished(
                IterationOutcome(1, 2, 5.0, "complete", "ses_1", "Done.")
            )
        out = stream.getvalue()
        self.assertIn("implementing", out)
        # The final painted status states the stage, not the tool it superseded.
        painted = [row for row in re.split(r"[\r\n]", out) if "iteration 1/2" in row]
        self.assertTrue(painted, out)
        self.assertIn("implementing", painted[-1])
        self.assertNotIn("Bash", painted[-1])


class _FakeTerminal(io.StringIO):
    """A stream that claims to be a terminal of a fixed width, for driving the
    status-line rendering deterministically where a real pseudo-terminal is not
    available. The width is honoured by patching ``os.get_terminal_size`` in the
    console module; ``fileno`` only has to answer, since the patched call ignores it."""

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return 1


class TerminalStatusLineTest(unittest.TestCase):
    """Drives the status line onto a fake terminal of a known width and asserts what
    lands there -- the fields it carries, that it repaints in place, that it fits each
    visual row, and that an interruption erases and redraws it -- never how it was
    painted."""

    def _drive(self, say: object, *, columns: int = 80, colour: bool = False) -> str:
        stream = _FakeTerminal()
        size = os.terminal_size((columns, 24))
        with mock.patch("ralph.console.os.get_terminal_size", return_value=size), \
                mock.patch.dict(os.environ, {}, clear=False):
            if colour:
                os.environ.pop("NO_COLOR", None)
            else:
                os.environ["NO_COLOR"] = "1"
            say(StreamRunConsole(stream))  # type: ignore[operator]
        return stream.getvalue()

    def test_the_status_line_repaints_in_place_and_carries_the_fields(self) -> None:
        def say(console: StreamRunConsole) -> None:
            console.iteration_started(1, 4)
            console.observe(ToolObserved("Bash"))
            console.observe(SubagentsObserved(("a", "b")))
            console.observe(ContextObserved(12483))
            console.iteration_finished(
                IterationOutcome(1, 4, 5.0, "complete", "ses_1", "Done.")
            )

        out = self._drive(say)
        self.assertIn("Bash", out)
        self.assertIn("2 subagents", out)
        self.assertIn("context 12483 tokens", out)
        # Repainted in place with carriage returns, not one appended line per update.
        self.assertIn("\r", out)
        for row in re.split(r"[\r\n]", out):
            self.assertLessEqual(len(without_ansi(row)), 80, f"{row!r} would wrap")
        # Erased before the outcome block, which stands clean at the start of a row.
        self.assertIn("ralph: iteration 1 of 4 complete", out)

    def test_an_opencode_style_run_carries_the_same_fields_minus_the_subagent_count(
        self,
    ) -> None:
        # OpenCode reports tool use and context but never a subagent roster, so its
        # status line carries the same fields with the same meanings as Claude's,
        # minus the subagent count -- rendered absent, never a fabricated "0 subagents"
        # (register G4/G5, #41).
        def say(console: StreamRunConsole) -> None:
            console.iteration_started(1, 4)
            console.observe(ToolObserved("bash"))
            console.observe(ContextObserved(22086))
            console.iteration_finished(
                IterationOutcome(1, 4, 5.0, "complete", "ses_1", "Done.")
            )

        out = self._drive(say)
        self.assertIn("bash", out)
        self.assertIn("context 22086 tokens", out)
        self.assertNotIn("subagent", out)

    def test_no_color_on_a_terminal_status_line_emits_no_escape(self) -> None:
        def say(console: StreamRunConsole) -> None:
            console.iteration_started(1, 1)
            console.observe(ToolObserved("Bash"))
            console.iteration_finished(
                IterationOutcome(1, 1, 1.0, "complete", "ses_1", "Done.")
            )

        self.assertNotIn("\033", self._drive(say, colour=False))

    def test_a_coloured_terminal_paints_the_status_line(self) -> None:
        def say(console: StreamRunConsole) -> None:
            console.iteration_started(1, 1)
            console.observe(ToolObserved("Bash"))
            console.iteration_finished(
                IterationOutcome(1, 1, 1.0, "complete", "ses_1", "Done.")
            )

        self.assertIn("\033[", self._drive(say, colour=True))

    def test_a_warning_erases_and_redraws_the_status_line(self) -> None:
        def say(console: StreamRunConsole) -> None:
            console.iteration_started(1, 2)
            console.observe(ToolObserved("Bash"))
            console.observe(MarkerWithdrawn("Should I pick option A?"))
            console.iteration_finished(
                IterationOutcome(1, 2, 1.0, "budget_exhausted", "ses_1", "Continuing.")
            )

        out = self._drive(say)
        # The interruption is a whole line an operator greps for; like a deviation or
        # a failure it is emitted whole and may wrap -- only the status line may not
        # (register G19). So the warning survives word for word...
        self.assertIn("withdrew it", out)
        self.assertIn("Should I pick option A?", out)
        # ...and the status line was redrawn after it (the tool reappears past the
        # warning), each of its in-place segments still fitting the window.
        after_warning = out.split("withdrew it", 1)[1]
        self.assertIn("Bash", after_warning)
        status_segments = [
            segment
            for segment in re.split(r"[\r\n]", out)
            if segment.startswith("ralph: iteration ")
        ]
        for segment in status_segments:
            self.assertLessEqual(len(without_ansi(segment)), 80, f"{segment!r} would wrap")

    def test_a_feed_line_erases_and_redraws_the_status_line(self) -> None:
        # An un-redirected ``--verbose`` puts the dashboard and the feed on the same
        # terminal, so a feed line must be given the same treatment as an operator
        # line: the status is cleared before it and redrawn after (register G3).
        terminal = _FakeTerminal()
        size = os.terminal_size((80, 24))
        with mock.patch("ralph.console.os.get_terminal_size", return_value=size), \
                mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=False):
            console = StreamRunConsole(terminal, feed=terminal)
            console.run_started(_settings(backend="opencode"))
            console.iteration_started(1, 2)
            console.observe(ToolObserved("Bash"))
            console.observe(Narrated("Reading the issue."))
            console.iteration_finished(
                IterationOutcome(1, 2, 1.0, "complete", "ses_1", "Done.")
            )
        out = terminal.getvalue()
        # The feed line starts a row of its own rather than landing on the status...
        rows = [row for row in re.split(r"[\r\n]", out) if row.strip()]
        self.assertIn("opencode: Reading the issue.", rows)
        # ...and the status line is back afterwards, so the clock keeps showing.
        self.assertIn("Bash", out.split("Reading the issue.", 1)[1])

    def test_a_narrow_terminal_drops_status_fields_rather_than_wrapping(self) -> None:
        def say(console: StreamRunConsole) -> None:
            console.iteration_started(1, 4)
            console.observe(ToolObserved("Bash"))
            console.observe(SubagentsObserved(("a", "b")))
            console.observe(ContextObserved(12483))
            console.iteration_finished(
                IterationOutcome(1, 4, 5.0, "complete", "ses_1", "Done.")
            )

        out = self._drive(say, columns=32)
        for row in re.split(r"[\r\n]", out):
            self.assertLessEqual(len(without_ansi(row)), 32, f"{row!r} would wrap")
        self.assertIn("iteration 1/4", out)


class PipedProgressTest(unittest.TestCase):
    """Off a terminal there is no in-place line: the ticker degrades to slow
    append-only heartbeats carrying the same facts and no ANSI (register G3/G11)."""

    def test_progress_degrades_to_append_only_heartbeats_with_no_ansi(self) -> None:
        stream = io.StringIO()
        console = StreamRunConsole(stream)
        with mock.patch("ralph.console.STATUS_TICK_SECONDS", 0.02), \
                mock.patch("ralph.console.HEARTBEAT_SECONDS", 0.02):
            console.iteration_started(1, 2)
            console.observe(ToolObserved("Bash"))
            console.observe(ContextObserved(500))
            # Let the real ticker emit at least one periodic heartbeat.
            time.sleep(0.2)
            console.iteration_finished(
                IterationOutcome(1, 2, 1.0, "complete", "ses_1", "Done.")
            )
        out = stream.getvalue()
        # No in-place repaint and no escape off a terminal.
        self.assertNotIn("\r", out)
        self.assertNotIn("\033", out)
        # A heartbeat line carried the same progress facts, append-only.
        self.assertTrue(
            any(
                "iteration 1/2" in line
                and "Bash" in line
                and "context 500 tokens" in line
                for line in out.split("\n")
            ),
            out,
        )

    def test_an_iteration_that_emits_no_observations_prints_no_status_line(self) -> None:
        # An Iteration that emits nothing never paints a status line or a heartbeat,
        # so a Backend that reports no progress leaves the piped log exactly as before.
        stream = io.StringIO()
        console = StreamRunConsole(stream)
        with mock.patch("ralph.console.STATUS_TICK_SECONDS", 0.02):
            console.iteration_started(1, 1)
            time.sleep(0.1)
            console.iteration_finished(
                IterationOutcome(1, 1, 1.0, "complete", "ses_1", "Done.")
            )
        out = stream.getvalue()
        self.assertNotIn("iteration 1/1 ·", out)


class BackendFeedTest(unittest.TestCase):
    """The Backend's running commentary: dropped from the default view (register G2)
    and restored, with a speaker prefix on every line, only when the operator opts in
    (register G11). Driven directly because the interleaving that makes the feed
    unreadable -- a Backend and its subagents narrating at once -- cannot be staged
    through a subprocess."""

    def _say(self, say: object, *, feed: bool = True) -> tuple[str, str]:
        dashboard = io.StringIO()
        transcript = io.StringIO()
        console = StreamRunConsole(dashboard, feed=transcript if feed else None)
        console.run_started(_settings(backend="claude"))
        say(console)  # type: ignore[operator]
        return dashboard.getvalue(), transcript.getvalue()

    def test_the_backend_commentary_is_silent_without_the_opt_in(self) -> None:
        dashboard, _ = self._say(
            lambda console: (
                console.observe(Narrated("Reading the issue.")),
                console.observe(ToolActivity("Read")),
                console.observe(StepObserved(started=True)),
            ),
            feed=False,
        )
        self.assertNotIn("Reading the issue.", dashboard)
        self.assertNotIn("[Read]", dashboard)
        self.assertNotIn("step started", dashboard)

    def test_the_feed_names_the_backend_or_the_subagent_that_spoke(self) -> None:
        _, feed = self._say(
            lambda console: (
                console.observe(Narrated("Delegating the survey.")),
                console.observe(Narrated("Survey done.", subagent="toolu_7")),
                console.observe(Narrated("Thanks.")),
            )
        )
        # Three concurrent monologues stay attributable line by line.
        self.assertEqual(
            feed.rstrip("\n").split("\n"),
            [
                "claude: Delegating the survey.",
                "claude/toolu_7: Survey done.",
                "claude: Thanks.",
            ],
        )

    def test_every_row_of_a_multi_line_passage_carries_its_speaker(self) -> None:
        _, feed = self._say(
            lambda console: console.observe(Narrated("First.\nSecond.", subagent="toolu_1"))
        )
        self.assertEqual(
            feed.rstrip("\n").split("\n"),
            ["claude/toolu_1: First.", "claude/toolu_1: Second."],
        )

    def test_streamed_fragments_are_joined_rather_than_prefixed_mid_sentence(
        self,
    ) -> None:
        _, feed = self._say(
            lambda console: (
                console.observe(Narrated("Work ", partial=True)),
                console.observe(Narrated("complete.\n", partial=True)),
            )
        )
        self.assertEqual(feed, "claude: Work complete.\n")

    def test_an_unfinished_line_is_still_shown_when_the_iteration_ends(self) -> None:
        _, feed = self._say(
            lambda console: (
                console.observe(Narrated("Trailing thought", partial=True)),
                console.iteration_finished(
                    IterationOutcome(1, 1, 1.0, "complete", "ses_1", "Done.")
                ),
            )
        )
        self.assertIn("claude: Trailing thought", feed)

    def test_progress_markers_carry_their_speaker_and_their_state(self) -> None:
        _, feed = self._say(
            lambda console: (
                console.observe(ToolActivity("Read")),
                console.observe(ToolActivity("Bash", subagent="toolu_2")),
                console.observe(ToolActivity("bash", state="completed")),
                console.observe(StepObserved(started=True)),
                console.observe(StepObserved(started=False)),
            )
        )
        self.assertEqual(
            feed.rstrip("\n").split("\n"),
            [
                "claude: [Read]",
                "claude/toolu_2: [Bash]",
                "claude: [bash (completed)]",
                "claude: [step started]",
                "claude: [step finished]",
            ],
        )

    def test_a_marker_never_lands_in_the_middle_of_a_streamed_sentence(self) -> None:
        _, feed = self._say(
            lambda console: (
                console.observe(Narrated("Now I will", partial=True)),
                console.observe(ToolActivity("Read")),
            )
        )
        self.assertEqual(
            feed.rstrip("\n").split("\n"),
            ["claude: Now I will", "claude: [Read]"],
        )

    def test_the_dashboard_and_the_feed_stay_on_separate_streams(self) -> None:
        dashboard, feed = self._say(
            lambda console: (
                console.observe(Narrated("Backend chatter.")),
                console.run_finished(
                    RunSummary(
                        outcome="complete",
                        run_dir=Path("/w/.git/ralph/runs/r1"),
                        initial_branch="main",
                        final_branch="main",
                        dirty=False,
                        upstream=None,
                        ahead=0,
                    )
                ),
            )
        )
        # Redirecting one leaves the other whole: neither stream carries the other's.
        self.assertIn("ralph: outcome run complete", dashboard)
        self.assertNotIn("Backend chatter.", dashboard)
        self.assertIn("claude: Backend chatter.", feed)
        self.assertNotIn("ralph: outcome", feed)

    def test_the_feed_is_redacted_like_every_other_operator_facing_line(self) -> None:
        secret = "oauth-subscription-token-1234567890"
        with mock.patch("ralph.console.redact", lambda text: text.replace(secret, "[redacted]")):
            _, feed = self._say(
                lambda console: console.observe(Narrated(f"Token {secret} used."))
            )
        self.assertNotIn(secret, feed)
        self.assertIn("[redacted]", feed)

    def test_ralph_paints_no_ansi_onto_the_feed_beside_a_coloured_dashboard(
        self,
    ) -> None:
        # Ralph adds no colour to the feed (register G12), so nothing it writes there
        # can be read as its own voice and a redirected transcript stays clean of
        # Ralph's escapes whatever the dashboard is doing.
        dashboard_stream = _FakeTerminal()
        feed_stream = io.StringIO()
        size = os.terminal_size((100, 24))
        with mock.patch("ralph.console.os.get_terminal_size", return_value=size), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NO_COLOR", None)
            console = StreamRunConsole(dashboard_stream, feed=feed_stream)
            console.run_started(_settings(backend="claude"))
            console.observe(Narrated("Backend chatter."))
        self.assertIn("\033[", dashboard_stream.getvalue())
        self.assertNotIn("\033", feed_stream.getvalue())


class QuietConsoleTest(unittest.TestCase):
    """Quiet drops the status line and the Iteration blocks and nothing else, so an
    unattended run stays quiet without the operator losing their help (register G11)."""

    def _say(self, say: object) -> str:
        stream = io.StringIO()
        say(StreamRunConsole(stream, quiet=True))  # type: ignore[operator]
        return stream.getvalue()

    def test_quiet_drops_the_iteration_blocks(self) -> None:
        out = self._say(
            lambda console: (
                console.iteration_started(1, 4),
                console.iteration_finished(
                    IterationOutcome(1, 4, 5.0, "complete", "ses_1", "Done.")
                ),
            )
        )
        self.assertEqual(out, "")

    def test_quiet_keeps_the_header_the_summary_and_every_failure(self) -> None:
        out = self._say(
            lambda console: (
                console.run_started(_settings()),
                console.iteration_started(1, 4),
                console.deviation(Deviation(NO_SANDBOX_DEVIATION)),
                console.observe(UnmarkedQuestion("Which one?")),
                console.run_finished(
                    RunSummary(
                        outcome="budget_exhausted",
                        run_dir=Path("/w/.git/ralph/runs/r1"),
                        initial_branch="main",
                        final_branch="main",
                        dirty=False,
                        upstream=None,
                        ahead=0,
                    )
                ),
                console.budget_continue("ralph run prompt.md --iterations 4"),
                console.failed("something went wrong"),
            )
        )
        self.assertIn("ralph: backend opencode", out)
        self.assertIn("--unsafe-no-sandbox is set", out)
        self.assertIn("unmarked operator-directed", out)
        self.assertIn("iteration budget exhausted", out)
        self.assertIn("continue Ralph: ralph run prompt.md --iterations 4", out)
        self.assertIn("something went wrong", out)

    def test_quiet_and_the_feed_govern_different_streams(self) -> None:
        # The two flags are orthogonal: quiet turns the dashboard down, the feed turns
        # the commentary on, and asking for both gets exactly both (register G11).
        dashboard = io.StringIO()
        transcript = io.StringIO()
        console = StreamRunConsole(dashboard, feed=transcript, quiet=True)
        console.run_started(_settings())
        console.iteration_started(1, 1)
        console.observe(Narrated("Backend chatter."))
        console.iteration_finished(
            IterationOutcome(1, 1, 1.0, "complete", "ses_1", "Done.")
        )
        self.assertIn("opencode: Backend chatter.", transcript.getvalue())
        # Quiet still drops the Iteration blocks while keeping the header.
        self.assertIn("ralph: backend opencode", dashboard.getvalue())
        self.assertNotIn("iteration 1 of 1", dashboard.getvalue())

    def test_quiet_paints_no_status_line_and_appends_no_heartbeat(self) -> None:
        stream = _FakeTerminal()
        size = os.terminal_size((100, 24))
        with mock.patch("ralph.console.os.get_terminal_size", return_value=size), \
                mock.patch("ralph.console.STATUS_TICK_SECONDS", 0.02), \
                mock.patch("ralph.console.HEARTBEAT_SECONDS", 0.02):
            console = StreamRunConsole(stream, quiet=True)
            console.iteration_started(1, 2)
            console.observe(ToolObserved("Bash"))
            console.observe(ContextObserved(500))
            time.sleep(0.2)
            console.iteration_finished(
                IterationOutcome(1, 2, 1.0, "complete", "ses_1", "Done.")
            )
        self.assertEqual(stream.getvalue(), "")


class TerminalOwnershipTest(unittest.TestCase):
    """Register G13: the Run console is the only module permitted to write to a
    terminal. Every emit site has now migrated -- the Loop's, the failure help,
    ``resume``'s full-auto caveat, and last the two Backend feeds -- so the rule is
    asserted unconditionally, with no allowlist of stragglers left to keep."""

    def _writes_to_a_terminal(self, source: str) -> bool:
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            if isinstance(target, ast.Name) and target.id == "print":
                return True
            if (
                isinstance(target, ast.Attribute)
                and target.attr in {"write", "writelines"}
                and isinstance(target.value, ast.Attribute)
                and target.value.attr in {"stdout", "stderr"}
                and isinstance(target.value.value, ast.Name)
                and target.value.value.id == "sys"
            ):
                return True
        return False

    def test_no_module_outside_the_run_console_writes_to_a_terminal(self) -> None:
        offenders = {
            str(path.relative_to(self._package()))
            for path in sorted(self._package().rglob("*.py"))
            if self._writes_to_a_terminal(path.read_text(encoding="utf-8"))
        }
        self.assertEqual(
            offenders,
            set(),
            "a module gained an operator-facing write: emit the fact through the "
            "injected Run console instead (register G13)",
        )

    def test_only_the_command_line_entry_point_constructs_a_run_console(self) -> None:
        # Register G16: everything below the composition root depends on the
        # abstraction, which is why both rendering choices an operator makes -- the
        # opt-in feed and quiet -- are one construction in one module.
        constructors = {
            str(path.relative_to(self._package()))
            for path in sorted(self._package().rglob("*.py"))
            if any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "StreamRunConsole"
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            )
        }
        self.assertEqual(constructors, {"cli.py"})

    @staticmethod
    def _package() -> Path:
        return ROOT / "src" / "ralph"
