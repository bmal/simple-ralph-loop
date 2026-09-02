"""Resume of a handed-off session: sanitized full-auto relaunch, unsafe
recovery-environment refusal, and provider/model consistency."""

from __future__ import annotations

import json

from harness import RalphCliTestCase


class HandoffAndResumeTest(RalphCliTestCase):
    def test_resume_relaunches_sanitized_full_auto_backend(self) -> None:
        opencode = self.resume_ralph("opencode", "openai/gpt-5.6-sol", "ses_9")
        self.assertEqual(opencode.returncode, 0, opencode.stderr)
        resume_call = (self.calls / "opencode-resume").read_text()
        self.assertIn("--session ses_9", resume_call)
        self.assertIn("--auto", resume_call)
        self.assertIn("--model openai/gpt-5.6-sol", resume_call)
        self.assertIn(f"--dir {self.repo.resolve()}", resume_call)
        self.assertIn("-im", (self.calls / "caffeinate").read_text())
        resume_env = (self.calls / "opencode-resume-env").read_text()
        self.assertIn("OPENCODE_DISABLE_AUTOUPDATE=true", resume_env)
        self.assertIn("OPENCODE_CONFIG_CONTENT=", resume_env)
        self.assertIn("OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS=2147483647", resume_env)
        self.assertNotIn("OPENAI_API_KEY=", resume_env)
        # Effective routing/auth is re-proved before the interactive session.
        self.assertTrue((self.calls / "auth-count").exists())

        for path in self.calls.iterdir():
            path.unlink()
        claude = self.resume_ralph("claude", "claude-opus-5", "claude-session-1")
        self.assertEqual(claude.returncode, 0, claude.stderr)
        claude_call = (self.calls / "claude-resume").read_text()
        self.assertIn("--resume claude-session-1", claude_call)
        self.assertIn("--dangerously-skip-permissions", claude_call)
        self.assertIn("--model claude-opus-5", claude_call)
        self.assertIn("--setting-sources project --strict-mcp-config", claude_call)
        self.assertIn("-im", (self.calls / "caffeinate").read_text())
        claude_env = (self.calls / "claude-resume-env").read_text()
        self.assertIn("DISABLE_AUTOUPDATER=1", claude_env)
        self.assertIn("BASH_MAX_TIMEOUT_MS=2147483647", claude_env)
        self.assertNotIn("ANTHROPIC_API_KEY=", claude_env)
        self.assertTrue((self.calls / "claude-auth-count").exists())

    def test_resume_states_the_session_it_is_entering_before_it_hands_over(self) -> None:
        # ``resume`` replaces its own process with the interactive session, so
        # everything the operator is told about the handover has to be said first.
        result = self.resume_ralph("claude", "claude-opus-5", "claude-session-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("claude-session-1", result.stderr)
        self.assertIn("claude-opus-5", result.stderr)
        self.assertIn("subscription-only authentication", result.stderr)
        self.assertIn("customization isolation", result.stderr)
        self.assertIn("host isolation", result.stderr)
        # The header precedes the handover, and the handover happened.
        self.assertTrue((self.calls / "claude-resume").exists())

    def test_resume_says_host_isolation_is_not_enforced_when_it_is_not(self) -> None:
        confined = self.resume_ralph("opencode", "openai/gpt-5.6-sol", "ses_9")
        self.assertEqual(confined.returncode, 0, confined.stderr)
        unconfined = self.resume_ralph(
            "opencode", "openai/gpt-5.6-sol", "ses_9", "--unsafe-no-sandbox"
        )

        self.assertEqual(unconfined.returncode, 0, unconfined.stderr)
        # The relaxed guarantee stays loud, and the compact header states the status
        # rather than leaving the operator to read it out of an omission.
        self.assertIn("--unsafe-no-sandbox is set", unconfined.stderr)
        self.assertIn("host isolation not enforced", unconfined.stderr)
        self.assertNotIn("host isolation not enforced", confined.stderr)

    def test_resume_refuses_unsafe_recovery_environment(self) -> None:
        secret = "sk-live-secret-value"
        api = self.resume_ralph(
            "opencode", "openai/gpt-5.6-sol", "ses_1", env={"OPENAI_API_KEY": secret}
        )
        self.assertNotEqual(api.returncode, 0)
        self.assertIn("API credential", api.stderr)
        self.assertNotIn(secret, api.stdout + api.stderr)
        self.assertFalse((self.calls / "opencode-resume").exists())

        for path in self.calls.iterdir():
            path.unlink()
        changed = self.resume_ralph(
            "opencode",
            "openai/gpt-5.6-sol",
            "ses_1",
            env={"FAKE_AUTH": "OpenAI oauth\nAnthropic api"},
        )
        self.assertEqual(changed.returncode, 2)
        self.assertIn("unfamiliar or ambiguous", changed.stderr)
        self.assertFalse((self.calls / "opencode-resume").exists())

        for path in self.calls.iterdir():
            path.unlink()
        plugin_dir = self.repo / ".opencode" / "plugin"
        plugin_dir.mkdir(parents=True)
        plugin = self.resume_ralph("opencode", "openai/gpt-5.6-sol", "ses_1")
        self.assertNotEqual(plugin.returncode, 0)
        self.assertIn("external plugins or custom tools", plugin.stderr)
        self.assertFalse((self.calls / "opencode-resume").exists())
        plugin_dir.rmdir()
        (self.repo / ".opencode").rmdir()

        for path in self.calls.iterdir():
            path.unlink()
        settings = self.repo / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"apiKeyHelper": "paid-key-command"}), encoding="utf-8")
        customized = self.resume_ralph("claude", "claude-opus-5", "claude-session-1")
        self.assertNotEqual(customized.returncode, 0)
        self.assertIn("Claude customizations", customized.stderr)
        self.assertFalse((self.calls / "claude-resume").exists())

    def test_resume_rejects_provider_mismatched_model(self) -> None:
        result = self.resume_ralph("opencode", "anthropic/claude", "ses_1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("openai/", result.stderr)
        self.assertFalse((self.calls / "opencode-resume").exists())
