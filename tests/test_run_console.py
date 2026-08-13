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
import os
from pathlib import Path
import unittest
from unittest import mock

from harness import ROOT, PtyCapture, RalphCliTestCase
from ralph.console import (
    CLAUDE_AGENTS_DEVIATION,
    NO_SANDBOX_DEVIATION,
    OPENCODE_AGENTS_DEVIATION,
    Deviation,
    IterationOutcome,
    OperatorHelp,
    RunSettings,
    RunSummary,
    StreamRunConsole,
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


class TerminalOwnershipTest(unittest.TestCase):
    """Register G13: the Run console is the only module permitted to write to a
    terminal. The rule cannot be true until every emit site has migrated, so the
    modules that still hold one are named here explicitly. Each later ticket in the
    Run console program deletes its own entry; the last one deletes this test."""

    # Modules with an unmigrated operator-facing write, and what still holds them.
    NOT_YET_MIGRATED = {
        # the resume full-auto warning (the handoff, budget-exhausted, and backend-
        # failure help migrated to the Run console in #39; the run header, iteration
        # blocks, summary, and deviation warnings before it)
        "cli.py",
        # the Backend feed, the withdrawn/unmarked mid-run marker warnings, and the
        # killed-background-task report (#48) (the --unsafe-allow-agents deviation
        # migrated to the Run console in #39)
        "backends/claude.py",
        "backends/opencode.py",
    }

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

    def test_only_the_run_console_and_the_named_stragglers_write_to_a_terminal(
        self,
    ) -> None:
        offenders = {
            str(path.relative_to(self._package()))
            for path in sorted(self._package().rglob("*.py"))
            if self._writes_to_a_terminal(path.read_text(encoding="utf-8"))
        }
        self.assertEqual(
            offenders,
            self.NOT_YET_MIGRATED,
            "a module gained or lost an operator-facing write: migrate it to the "
            "Run console, or update the allowlist to name exactly what remains",
        )

    def test_only_the_command_line_entry_point_constructs_a_run_console(self) -> None:
        # Register G16: everything below the composition root depends on the
        # abstraction, so a later ticket can swap in a quiet or verbose renderer by
        # changing one line in one module.
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
