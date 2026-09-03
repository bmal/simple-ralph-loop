"""Launch chain: the sandbox wrap nested inside caffeinate, profile
generation under ralph state, and the backend-aware wrap boundary."""

from __future__ import annotations

import shlex

from harness import RalphCliTestCase


class LaunchChainTest(RalphCliTestCase):
    def test_opencode_launch_is_wrapped_by_sandbox_inside_caffeinate(self) -> None:
        # The backend runs as a child of sandbox-exec, which itself runs as a
        # child of caffeinate: caffeinate -im sandbox-exec -f <profile> opencode …
        # (register D6/D13, caffeinate outermost).
        result = self.run_ralph()

        self.assertEqual(result.returncode, 0, result.stderr)
        caffeinate = (self.calls / "caffeinate").read_text()
        wrap = next(
            line for line in caffeinate.splitlines() if "sandbox-exec" in line
        )
        self.assertTrue(wrap.startswith("-im "), wrap)
        sandbox = str(self.bin / "sandbox-exec")
        profiles = sorted(self._ralph_state().glob("runs/*/sandbox.sb"))
        self.assertEqual(len(profiles), 1, profiles)
        # The launch chain records the run directory's resolved path.
        profile = profiles[0].resolve()
        self.assertIn(f"-im {sandbox} -f {profile} opencode", wrap)
        # sandbox-exec received the profile then the confined backend command.
        # (The pre-loop self-test probes are recorded first; select the backend
        # launch line, which must still carry the profile then opencode's argv.)
        recorded = (self.calls / "sandbox-exec").read_text().splitlines()
        launch_line = next(line for line in recorded if "opencode --pure run" in line)
        self.assertTrue(launch_line.startswith(f"-f {profile} opencode --pure run"), launch_line)

    def test_sandbox_profile_is_written_under_ralph_state_and_confines_reads_and_writes(self) -> None:
        result = self.run_ralph()

        self.assertEqual(result.returncode, 0, result.stderr)
        profile = sorted(self._ralph_state().glob("runs/*/sandbox.sb"))[0]
        self.assertFalse(profile.is_symlink())
        text = profile.read_text()
        # The concrete profile carries the resolved worktree write root, the
        # famous read denials, and the owner-amended keychain rule.
        self.assertIn(f'(allow file-write* (subpath "{self.repo.resolve()}"))', text)
        self.assertIn(f'(deny file-read* (subpath "{self.home}/.ssh"))', text)
        self.assertIn(
            f'(allow file-read* (literal "{self.home}/Library/Keychains/login.keychain-db"))',
            text,
        )
        # It denies the out-of-scope Claude store for an OpenCode run.
        self.assertIn(f'(deny file-read* (subpath "{self.home}/.claude"))', text)

    def test_ralph_clean_removes_the_generated_sandbox_profile(self) -> None:
        self.run_ralph()
        self.assertTrue(sorted(self._ralph_state().glob("runs/*/sandbox.sb")))

        result = self.clean_ralph()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self._ralph_state().exists())

    def test_absolute_sandbox_exec_is_not_path_shadowed(self) -> None:
        # A hostile sandbox-exec earlier on PATH must never be consulted: host
        # isolation is resolved by absolute path only.
        system = self.base / "system"
        system.mkdir()
        good = system / "sandbox-exec"
        good.write_text((self.bin / "sandbox-exec").read_text(), encoding="utf-8")
        good.chmod(0o755)
        self._script(
            "sandbox-exec",
            """
            printf 'shadow\\n' >> "$FAKE_CALLS/sandbox-exec-shadow"
            exit 13
            """,
        )

        result = self.run_ralph(env={"RALPH_SANDBOX_EXEC": str(good)})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.calls / "sandbox-exec-shadow").exists())
        self.assertIn(str(good), (self.calls / "caffeinate").read_text())

    def test_sandbox_self_test_runs_once_per_run_before_the_first_iteration(self) -> None:
        # The self-test (register D8) probes the profile once per run — the
        # profile is stable across iterations — before any backend launch. Prove
        # both by running three iterations and checking the recorded sandbox-exec
        # calls: exactly one read probe and one write probe, both ahead of every
        # backend launch.
        sequence = self._sequence(
            [
                "Child one done.",
                "Child two done.",
                "No work remains.\n<promise>COMPLETE</promise>",
            ]
        )
        result = self.run_ralph(
            "--iterations", "3", env={"FAKE_SEQUENCE_DIR": str(sequence)}
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = (self.calls / "sandbox-exec").read_text().splitlines()
        read_probes = [i for i, line in enumerate(recorded) if "Library/Keychains" in line]
        write_probes = [
            i for i, line in enumerate(recorded) if ".ralph-sandbox-selftest-write-probe" in line
        ]
        launches = [i for i, line in enumerate(recorded) if "opencode --pure run" in line]
        self.assertEqual(len(read_probes), 1, recorded)
        self.assertEqual(len(write_probes), 1, recorded)
        self.assertEqual(len(launches), 3, recorded)
        # Both probes are recorded before the first backend launch.
        self.assertLess(max(read_probes + write_probes), min(launches), recorded)

    def test_sandbox_self_test_fail_open_stops_before_any_budget(self) -> None:
        # A profile that parses but fails open (here the fake's simulated
        # permitted read) must stop the run fail-closed before a single backend
        # invocation — no preflight, no session, no budget spent.
        result = self.run_ralph(env={"FAKE_SELFTEST_ALLOW": "read"})

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("host isolation self-test failed open", result.stderr)
        self.assertFalse(
            (self.calls / "opencode").exists(),
            "the backend must not be invoked when the self-test fails closed",
        )

    def test_sandbox_self_test_fail_open_write_stops_before_any_budget(self) -> None:
        result = self.run_ralph(env={"FAKE_SELFTEST_ALLOW": "write"})

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("host isolation self-test failed open", result.stderr)
        self.assertIn("write", result.stderr)
        self.assertFalse((self.calls / "opencode").exists())

    def test_sandbox_self_test_unavailable_probe_stops_before_any_budget(self) -> None:
        # If the probe cannot run at all (here sandbox-exec cannot be launched),
        # the run stops fail-closed rather than proceeding unproven.
        missing = self.base / "no-such-sandbox-exec"
        result = self.run_ralph(env={"RALPH_SANDBOX_EXEC": str(missing)})

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("host isolation self-test could not run", result.stderr)
        self.assertFalse((self.calls / "opencode").exists())

    def test_claude_launch_is_wrapped_by_sandbox_inside_caffeinate(self) -> None:
        # #22 confines Claude through the same shared launcher as OpenCode:
        # caffeinate -im sandbox-exec -f <profile> claude … (register D6/D13,
        # caffeinate outermost, one code path, not a Claude-specific fork).
        result = self.run_ralph(backend="claude")

        self.assertEqual(result.returncode, 0, result.stderr)
        caffeinate = (self.calls / "caffeinate").read_text()
        wrap = next(
            line for line in caffeinate.splitlines() if "sandbox-exec" in line
        )
        self.assertTrue(wrap.startswith("-im "), wrap)
        sandbox = str(self.bin / "sandbox-exec")
        profiles = sorted(self._ralph_state().glob("runs/*/sandbox.sb"))
        self.assertEqual(len(profiles), 1, profiles)
        profile = profiles[0].resolve()
        self.assertIn(f"-im {sandbox} -f {profile} claude -p", wrap)
        # sandbox-exec received the profile then the confined Claude command.
        recorded = (self.calls / "sandbox-exec").read_text().splitlines()
        launch_line = next(line for line in recorded if "claude -p" in line)
        self.assertTrue(launch_line.startswith(f"-f {profile} claude -p"), launch_line)

    def test_claude_run_profile_is_backend_aware(self) -> None:
        # For a Claude run the same generator flips the backend-aware inputs
        # (register D4/D6): the in-scope store is ~/.claude (a write root and left
        # readable) and the out-of-scope store denied is OpenCode's auth file.
        result = self.run_ralph(backend="claude")

        self.assertEqual(result.returncode, 0, result.stderr)
        text = sorted(self._ralph_state().glob("runs/*/sandbox.sb"))[0].read_text()
        self.assertIn(f'(allow file-write* (subpath "{self.home}/.claude"))', text)
        self.assertIn(
            f'(deny file-read* (literal "{self.home}/.local/share/opencode/auth.json"))',
            text,
        )
        self.assertNotIn(f'(deny file-read* (subpath "{self.home}/.claude"))', text)


class RecoveryCommandReproductionTest(RalphCliTestCase):
    """Every recovery command Ralph prints is the run's own invocation again, so an
    operator who runs it gets the run they had. A flag that changed what the run did
    and is missing from the printed command is a defect, not a policy: the operator
    silently loses a credential lane, an interactive-only label, or a timeout they
    declared. Only ``--verbose`` and ``--quiet`` are deliberately absent -- they
    govern rendering, not what a run does."""

    LABEL = "owner-decision"
    MODEL = "claude-sonnet-4-6"
    UNFINISHED = "Still working on it."
    QUESTION = "<promise>NEEDS_INPUT</promise>\nWhich option should I use?"

    # Every ``ralph run`` flag that changes what a run does, at a value the default
    # would not produce, plus the two display flags that must be reproduced nowhere.
    EVERY_FLAG = (
        "--iterations",
        "3",
        "--model",
        MODEL,
        "--timeout",
        "120",
        "--interactive-label",
        LABEL,
        "--unsafe-allow-agents",
        "--unsafe-no-sandbox",
        "--in-scope-backend",
        "opencode",
        "--verbose",
    )

    def _line(self, stderr: str, anchor: str) -> str:
        return next(
            line.removeprefix(anchor)
            for line in stderr.splitlines()
            if line.startswith(anchor)
        )

    def _exhausted(self, *extra: str) -> str:
        """The ``continue Ralph:`` line of a run that spent its whole budget."""
        result = self.run_ralph(
            *extra,
            env={
                "FAKE_EVENTS": self._events(self.UNFINISHED),
                "FAKE_EXPORT": self._export(self.UNFINISHED),
            },
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("iteration budget exhausted", result.stderr)
        return self._line(result.stderr, "continue Ralph: ")

    def _handed_off(self, *extra: str, model: str = MODEL) -> tuple[str, str]:
        """The ``manual resume:`` and ``continue Ralph:`` lines of a handed-off run."""
        result = self.run_ralph(
            *extra,
            backend="claude",
            env={"FAKE_CLAUDE_EVENTS": self._claude_events(self.QUESTION, model=model)},
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        return (
            self._line(result.stderr, "manual resume: "),
            self._line(result.stderr, "continue Ralph: "),
        )

    def test_budget_exhaustion_reproduces_the_declared_in_scope_backend(self) -> None:
        # The review's finding 4, reproduced: the run states the in-scope deviation
        # loudly and then hands back a continuation command that has dropped the very
        # lane the operator declared, so the work the declaration existed for cannot
        # start.
        restart = self._exhausted("--in-scope-backend", "claude")

        self.assertIn("--in-scope-backend claude", restart)

    def test_budget_exhaustion_reproduces_the_interactive_label(self) -> None:
        # The review's finding 12: continuing under the default label treats a
        # different set of children as blocked than the run the operator started.
        restart = self._exhausted("--interactive-label", self.LABEL)

        self.assertIn(f"--interactive-label {self.LABEL}", restart)

    def test_handoff_reproduces_the_interactive_label(self) -> None:
        _resume, restart = self._handed_off(
            "--iterations", "2", "--interactive-label", self.LABEL, model="claude-opus-5"
        )

        self.assertIn(f"--interactive-label {self.LABEL}", restart)

    def test_every_run_affecting_flag_round_trips_into_the_recovery_commands(self) -> None:
        # One run with every run-affecting flag off its default, checked flag by flag
        # against both anchors. Each flag appears in every recovery command whose
        # subcommand accepts it: ``ralph resume`` enters an interactive session and
        # takes neither a budget, a timer, nor the Loop protocol's label.
        resume, restart = self._handed_off(*self.EVERY_FLAG)

        for expected in (
            "--backend claude",
            # The budget is the one value a continuation does not copy verbatim: the
            # handoff consumed the first of three iterations, so two are offered.
            "--iterations 2",
            f"--model {self.MODEL}",
            "--timeout 120.0",
            f"--worktree {self.repo.resolve()}",
            f"--interactive-label {self.LABEL}",
            "--unsafe-allow-agents",
            "--unsafe-no-sandbox",
            "--in-scope-backend opencode",
        ):
            self.assertIn(expected, restart, f"{expected} missing from continue Ralph:")
        for expected in (
            "--backend claude",
            f"--model {self.MODEL}",
            f"--worktree {self.repo.resolve()}",
            "--unsafe-allow-agents",
            "--unsafe-no-sandbox",
            "--in-scope-backend opencode",
        ):
            self.assertIn(expected, resume, f"{expected} missing from manual resume:")
        # The prompt the run snapshotted is the prompt the continuation runs.
        self.assertIn(str(self.prompt.resolve()), restart)
        # Display-only flags govern rendering, not the run: reproduced nowhere.
        for display in ("--verbose", "--quiet"):
            self.assertNotIn(display, resume, display)
            self.assertNotIn(display, restart, display)

    def test_budget_exhaustion_restores_the_whole_budget(self) -> None:
        # The other half of the budget rule: nothing was left of the budget, so the
        # continuation is the run again with the whole of it, not a zero-iteration
        # command the operator has to repair.
        restart = self._exhausted("--iterations", "2")

        self.assertIn("--iterations 2", restart)

    def test_no_flag_can_be_forgotten_from_a_recovery_command(self) -> None:
        # The structural half of findings 4 and 12: both were one flag added without
        # its recovery-command line. This drives the parser itself, so a tenth flag
        # that changes what a run does fails here until it round-trips -- being
        # remembered this time is not the property under test.
        from ralph.cli import parser

        subparsers = parser()._subparsers._group_actions[0].choices
        options = {
            name: {
                option
                for action in subparsers[name]._actions
                for option in action.option_strings
                if option.startswith("--")
            }
            for name in ("run", "resume")
        }
        resume, restart = self._handed_off(*self.EVERY_FLAG)

        # Matched as whole arguments, not as substrings: ``--backend`` occurs inside
        # ``--in-scope-backend``, so a substring check would call a dropped flag
        # reproduced.
        printed = {name: set(shlex.split(line)) for name, line in
                   (("run", restart), ("resume", resume))}
        # ``--help`` is argparse's own; the display pair is the documented omission.
        for flag in sorted(options["run"] - {"--help", "--verbose", "--quiet"}):
            self.assertIn(
                flag, printed["run"], f"{flag} is not reproduced into continue Ralph:"
            )
        for flag in sorted(options["resume"] - {"--help"}):
            self.assertIn(
                flag, printed["resume"], f"{flag} is not reproduced into manual resume:"
            )

    def test_the_resumed_session_stays_the_last_argument(self) -> None:
        # ``launch`` promises callers that ``--session`` is placed last; a growing
        # flag list must not push it into the middle.
        resume, _restart = self._handed_off(*self.EVERY_FLAG)

        _cd, arguments = resume.split(" && ", 1)
        self.assertEqual(shlex.split(arguments)[-2:], ["--session", "claude-session-1"])
