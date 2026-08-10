"""The Run console: the run header an operator is shown before budget is spent,
the terminal and piped rendering paths, and the structural rule that no other
module addresses a terminal.

Two tiers, as the program's testing decisions require. The black-box CLI seam
asserts what a real run tells an operator; the unit tests below drive the console
directly against a real pseudo-terminal of a known width, because width
truncation and colour suppression are miserable to prove through a subprocess."""

from __future__ import annotations

import ast
import os
from pathlib import Path
import unittest
from unittest import mock

from harness import ROOT, PtyCapture, RalphCliTestCase
from ralph.console import RunSettings, StreamRunConsole


def without_ansi(text: str) -> str:
    """What the same output would have been on a stream that is not a terminal --
    the only form worth asserting on, since colour is a rendering detail and the
    facts are not."""
    while "\033[" in text:
        start = text.index("\033[")
        text = text[:start] + text[text.index("m", start) + 1 :]
    return text


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
        # the iteration banner, the branch-change warning, the handoff banner, and
        # the budget-exhausted line
        "loop.py",
        # the resume full-auto warning
        "cli.py",
        # the --unsafe-no-sandbox warning
        "launch.py",
        # the --unsafe-allow-agents warning and the Backend feed
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
