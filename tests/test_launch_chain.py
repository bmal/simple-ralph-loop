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
        recorded = (self.calls / "sandbox-exec").read_text().strip()
        self.assertTrue(recorded.startswith(f"-f {profile} opencode --pure run"), recorded)

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

    def test_claude_launch_is_not_yet_sandboxed(self) -> None:
        # #20 wraps only OpenCode; the Claude wrap lands in #22. Guard the
        # boundary so the Claude path is untouched until then.
        result = self.run_ralph(backend="claude")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.calls / "sandbox-exec").exists())
        self.assertFalse(sorted(self._ralph_state().glob("runs/*/sandbox.sb")))
