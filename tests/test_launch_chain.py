"""Launch chain: the sandbox wrap nested inside caffeinate, profile
generation under ralph state, and the backend-aware wrap boundary."""

from __future__ import annotations

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

    def test_claude_launch_is_not_yet_sandboxed(self) -> None:
        # #20 wraps only OpenCode; the Claude wrap lands in #22. Guard the
        # boundary so the Claude path is untouched until then.
        result = self.run_ralph(backend="claude")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.calls / "sandbox-exec").exists())
        self.assertFalse(sorted(self._ralph_state().glob("runs/*/sandbox.sb")))
