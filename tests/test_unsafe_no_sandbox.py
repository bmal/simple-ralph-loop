"""The --unsafe-no-sandbox opt-out: it disables the host-isolation wrap and its
self-test, is orthogonal to --unsafe-allow-agents, fails closed by default,
sandboxes resume identically, and reproduces into recovery commands only when
set (register D7/D9)."""

from __future__ import annotations

import json
import shlex

from harness import RalphCliTestCase


class UnsafeNoSandboxRunTest(RalphCliTestCase):
    def _caffeinate_launch_line(self) -> str:
        # The backend-launch invocation of caffeinate (the one that execs the
        # backend), never the loop-wide `-im -w <pid>` power assertion.
        lines = [
            line
            for line in (self.calls / "caffeinate").read_text().splitlines()
            if " -w " not in line and not line.endswith(" -w")
        ]
        launches = [line for line in lines if "opencode" in line or "claude" in line]
        self.assertEqual(len(launches), 1, self.calls_caffeinate())
        return launches[0]

    def calls_caffeinate(self) -> str:
        return (self.calls / "caffeinate").read_text()

    def test_flag_disables_wrap_and_self_test_with_a_loud_warning(self) -> None:
        result = self.run_ralph("--unsafe-no-sandbox")

        self.assertEqual(result.returncode, 0, result.stderr)
        # No profile is generated and none is written under ralph state.
        self.assertEqual(sorted(self._ralph_state().glob("runs/*/sandbox.sb")), [])
        # sandbox-exec is never invoked: neither the launch wrap nor the
        # self-test probes touch it.
        self.assertFalse((self.calls / "sandbox-exec").exists())
        # The backend launches directly under caffeinate, with no sandbox-exec
        # wrap between the -im assertion and the backend command.
        launch = self._caffeinate_launch_line()
        self.assertNotIn("sandbox-exec", launch)
        self.assertTrue(launch.startswith("-im opencode "), launch)
        # A loud stderr warning states host isolation is not being proven.
        self.assertIn("--unsafe-no-sandbox is set", result.stderr)
        self.assertIn("NOT proving host isolation", result.stderr)
        # The backend still ran to completion.
        self.assertTrue((self.calls / "opencode").exists())

    def test_default_run_wraps_and_probes_the_sandbox(self) -> None:
        # Regression: without the flag the wrap and self-test are present, so the
        # flag's absence keeps host isolation on.
        result = self.run_ralph()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(sorted(self._ralph_state().glob("runs/*/sandbox.sb"))), 1)
        self.assertTrue((self.calls / "sandbox-exec").exists())
        self.assertIn("sandbox-exec", self._caffeinate_launch_line())
        self.assertNotIn("--unsafe-no-sandbox is set", result.stderr)

    def test_flag_is_orthogonal_to_unsafe_allow_agents(self) -> None:
        # Both flags set at once: the sandbox is disabled and the agent vector is
        # relaxed, independently. A .claude/agents repo run under Claude proceeds.
        agents = self.repo / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "custom.md").write_text("agent", encoding="utf-8")

        both = self.run_ralph(
            "--unsafe-allow-agents", "--unsafe-no-sandbox", backend="claude"
        )
        self.assertEqual(both.returncode, 0, both.stderr)
        self.assertIn("--unsafe-no-sandbox is set", both.stderr)
        self.assertIn("Ralph is not proving Claude subagent isolation", both.stderr)
        self.assertFalse((self.calls / "sandbox-exec").exists())
        self.assertTrue((self.calls / "claude").exists())

    def test_flag_relaxes_only_host_isolation_not_customization(self) -> None:
        # --unsafe-no-sandbox does not relax customization isolation: a Claude
        # agents repo with no --unsafe-allow-agents is still refused, and the
        # backend never launches.
        agents = self.repo / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "custom.md").write_text("agent", encoding="utf-8")

        result = self.run_ralph("--unsafe-no-sandbox", backend="claude")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Claude customizations", result.stderr)
        self.assertFalse((self.calls / "claude").exists())

    def test_flag_leaves_the_full_auto_warning_in_place(self) -> None:
        # Disabling host isolation changes no other warning: the dangerous
        # full-auto warning still fires.
        result = self.run_ralph("--unsafe-no-sandbox")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dangerous full-auto", result.stderr)

    def test_flag_does_not_relax_the_runtime_init_event_isolation(self) -> None:
        # The flag governs host isolation only: an init event that loads external
        # MCP servers still fails closed at the runtime isolation assertion, even
        # though the sandbox is disabled.
        event_lines = self._claude_events("Done").splitlines()
        init = json.loads(event_lines[0])
        init["mcp_servers"] = [{"name": "external"}]
        event_lines[0] = json.dumps(init)

        result = self.run_ralph(
            "--unsafe-no-sandbox",
            backend="claude",
            env={"FAKE_CLAUDE_EVENTS": "\n".join(event_lines)},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("external MCP servers or plugins", result.stderr)

    def test_flag_does_not_relax_secret_redaction(self) -> None:
        # Redaction is untouched: a leaked subscription token is still scrubbed
        # from the streamed output when the sandbox is disabled.
        token = "oauth-subscription-token-1234567890"
        text = f"Token is {token} inside output.\nDone.\n<promise>COMPLETE</promise>"
        result = self.run_ralph(
            "--unsafe-no-sandbox",
            backend="claude",
            env={
                "CLAUDE_CODE_OAUTH_TOKEN": token,
                "FAKE_CLAUDE_EVENTS": self._claude_events(text),
                "FAKE_CLAUDE_LEAK_STDERR": "1",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(token, result.stdout + result.stderr)
        self.assertIn("Token is [redacted] inside output.", result.stdout)


class UnsafeNoSandboxResumeTest(RalphCliTestCase):
    def _resume_profile(self) -> list:
        return sorted(self._ralph_state().glob("resume/sandbox.sb"))

    def test_resume_is_sandboxed_identically_by_default(self) -> None:
        result = self.resume_ralph("opencode", "openai/gpt-5.6-sol", "ses_9")

        self.assertEqual(result.returncode, 0, result.stderr)
        # The resume argv is wrapped: caffeinate -im sandbox-exec -f <profile> …
        caffeinate = (self.calls / "caffeinate").read_text()
        wrap = next(line for line in caffeinate.splitlines() if "sandbox-exec" in line)
        self.assertTrue(wrap.startswith("-im "), wrap)
        profiles = self._resume_profile()
        self.assertEqual(len(profiles), 1, profiles)
        profile = profiles[0].resolve()
        sandbox = str(self.bin / "sandbox-exec")
        self.assertIn(f"-im {sandbox} -f {profile} opencode", wrap)
        # The confined resume command still reaches the backend.
        self.assertIn("--session ses_9", (self.calls / "opencode-resume").read_text())

    def test_resume_flag_disables_the_wrap_with_a_warning(self) -> None:
        result = self.resume_ralph(
            "opencode", "openai/gpt-5.6-sol", "ses_9", "--unsafe-no-sandbox"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--unsafe-no-sandbox is set", result.stderr)
        self.assertEqual(self._resume_profile(), [])
        self.assertFalse((self.calls / "sandbox-exec").exists())
        caffeinate = (self.calls / "caffeinate").read_text()
        self.assertNotIn("sandbox-exec", caffeinate)
        # The backend still resumes, just unconfined.
        self.assertIn("--session ses_9", (self.calls / "opencode-resume").read_text())


class UnsafeNoSandboxHandoffReproductionTest(RalphCliTestCase):
    QUESTION = "<promise>NEEDS_INPUT</promise>\nWhich option should I use?"

    def _handoff_commands(self, *extra: str) -> tuple[str, str]:
        result = self.run_ralph(
            "--iterations",
            "2",
            *extra,
            env={
                "FAKE_EVENTS": self._events(self.QUESTION),
                "FAKE_EXPORT": self._export(self.QUESTION),
            },
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        resume = next(
            line.removeprefix("manual resume: ")
            for line in result.stderr.splitlines()
            if line.startswith("manual resume: ")
        )
        restart = next(
            line.removeprefix("continue Ralph: ")
            for line in result.stderr.splitlines()
            if line.startswith("continue Ralph: ")
        )
        return resume, restart

    def test_flag_reproduced_into_both_recovery_commands_when_set(self) -> None:
        resume, restart = self._handoff_commands("--unsafe-no-sandbox")
        self.assertIn("--unsafe-no-sandbox", resume)
        self.assertIn("--unsafe-no-sandbox", restart)
        # --session remains the final argument of the resume command.
        _cd, resume_args = resume.split(" && ", 1)
        self.assertEqual(shlex.split(resume_args)[-2:], ["--session", "ses_1"])

    def test_flag_absent_from_recovery_commands_when_unset(self) -> None:
        resume, restart = self._handoff_commands()
        self.assertNotIn("--unsafe-no-sandbox", resume)
        self.assertNotIn("--unsafe-no-sandbox", restart)

    def test_two_unsafe_flags_reproduce_independently(self) -> None:
        # allow-agents only: no-sandbox must not leak into the commands.
        resume, restart = self._handoff_commands("--unsafe-allow-agents")
        self.assertIn("--unsafe-allow-agents", resume)
        self.assertIn("--unsafe-allow-agents", restart)
        self.assertNotIn("--unsafe-no-sandbox", resume)
        self.assertNotIn("--unsafe-no-sandbox", restart)

        for path in self.calls.iterdir():
            path.unlink()

        # no-sandbox only: allow-agents must not leak into the commands.
        resume, restart = self._handoff_commands("--unsafe-no-sandbox")
        self.assertIn("--unsafe-no-sandbox", resume)
        self.assertIn("--unsafe-no-sandbox", restart)
        self.assertNotIn("--unsafe-allow-agents", resume)
        self.assertNotIn("--unsafe-allow-agents", restart)

        for path in self.calls.iterdir():
            path.unlink()

        # Both set: both reproduce, with --session still last on resume.
        resume, restart = self._handoff_commands(
            "--unsafe-allow-agents", "--unsafe-no-sandbox"
        )
        self.assertIn("--unsafe-allow-agents", resume)
        self.assertIn("--unsafe-no-sandbox", resume)
        self.assertIn("--unsafe-allow-agents", restart)
        self.assertIn("--unsafe-no-sandbox", restart)
        _cd, resume_args = resume.split(" && ", 1)
        self.assertEqual(shlex.split(resume_args)[-2:], ["--session", "ses_1"])
