"""Claude adapter preflight: subscription-safe headless mode, model and
auth-version validation, customization and managed-configuration refusal."""

from __future__ import annotations

import json

from harness import RalphCliTestCase


class ClaudePreflightTest(RalphCliTestCase):
    def test_claude_completion_uses_subscription_safe_headless_mode(self) -> None:
        result = self.run_ralph(backend="claude")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Work complete.", result.stdout)
        invocation = (self.calls / "claude").read_text()
        self.assertIn("-p --input-format stream-json --output-format stream-json", invocation)
        self.assertIn("--dangerously-skip-permissions", invocation)
        self.assertIn("--model claude-opus-4-8", invocation)
        self.assertIn("--setting-sources project --strict-mcp-config", invocation)
        self.assertNotIn("--bare", invocation)
        child_env = (self.calls / "claude-env").read_text()
        self.assertIn("DISABLE_AUTOUPDATER=1", child_env)
        self.assertIn("BASH_MAX_TIMEOUT_MS=2147483647", child_env)
        auth_env = (self.calls / "claude-auth-env").read_text()
        self.assertNotIn("ANTHROPIC_API_KEY=", auth_env)
        self.assertNotIn("ANTHROPIC_CUSTOM_HEADERS=", auth_env)
        composed = json.loads((self.calls / "claude-stdin").read_text())
        self.assertIn("Implement the selected issue.", composed["message"]["content"])
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        session = json.loads((run_dir / "session.json").read_text())
        self.assertEqual(session["session_id"], "claude-session-1")
        self.assertEqual(session["initial_model"], "claude-opus-4-8")
        self.assertEqual(session["fallback_models"], [])
        self.assertIn("claude diagnostic", (run_dir / "stderr.log").read_text())

    def test_claude_accepts_explicit_model_and_records_transient_fallback(self) -> None:
        requested = "claude-sonnet-4-6"
        events = self._claude_events("Implemented.", model=requested)
        assistant = json.loads(events.splitlines()[1])
        assistant["message"]["model"] = "claude-sonnet-4-5"
        event_lines = events.splitlines()
        event_lines[1] = json.dumps(assistant)

        result = self.run_ralph(
            "--model",
            requested,
            backend="claude",
            env={"FAKE_CLAUDE_EVENTS": "\n".join(event_lines)},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("iteration budget exhausted", result.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        session = json.loads((run_dir / "session.json").read_text())
        self.assertEqual(session["fallback_models"], ["claude-sonnet-4-5"])

    def test_claude_rejects_unsafe_auth_version_and_initial_model(self) -> None:
        cases = [
            ({"FAKE_CLAUDE_VERSION": "2.1.207"}, "2.1.208"),
            (
                {
                    "FAKE_CLAUDE_AUTH": json.dumps(
                        {"loggedIn": True, "authMethod": "console", "apiProvider": "firstParty"}
                    )
                },
                "subscription OAuth",
            ),
            (
                {
                    "CLAUDE_CODE_OAUTH_TOKEN": "team-token",
                    "FAKE_CLAUDE_AUTH": json.dumps(
                        {
                            "loggedIn": True,
                            "authMethod": "claude.ai",
                            "apiProvider": "firstParty",
                            "subscriptionType": "team",
                        }
                    )
                },
                "subscription OAuth",
            ),
            (
                {"FAKE_CLAUDE_EVENTS": self._claude_events("Done", model="claude-sonnet-4-6")},
                "initial model",
            ),
        ]
        for environment, message in cases:
            with self.subTest(environment=environment):
                result = self.run_ralph(backend="claude", env=environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                for path in self.calls.iterdir():
                    path.unlink()

    def test_claude_rejects_customizations_and_malformed_streams(self) -> None:
        agents = self.repo / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "unsafe.md").write_text("custom agent", encoding="utf-8")
        customized = self.run_ralph(backend="claude")
        self.assertNotEqual(customized.returncode, 0)
        self.assertIn("Claude customizations", customized.stderr)
        self.assertFalse((self.calls / "claude").exists())

        (agents / "unsafe.md").unlink()
        agents.rmdir()
        settings = self.repo / ".claude" / "settings.json"
        settings.write_text(json.dumps({"apiKeyHelper": "paid-key-command"}), encoding="utf-8")
        helper = self.run_ralph(backend="claude")
        self.assertNotEqual(helper.returncode, 0)
        self.assertIn("Claude customizations", helper.stderr)

        settings.unlink()
        (self.repo / ".claude").rmdir()
        malformed = self.run_ralph(backend="claude", env={"FAKE_CLAUDE_EVENTS": "not-json"})
        self.assertNotEqual(malformed.returncode, 0)
        self.assertIn("malformed structured output", malformed.stderr)

    def test_claude_handoff_reproduces_flag_with_session_last(self) -> None:
        events = self._claude_events("unused").splitlines()
        assistant = json.loads(events[1])
        assistant["message"]["content"] = [
            {
                "type": "tool_use",
                "name": "AskUserQuestion",
                "input": {"questions": [{"question": "Which migration path should I take?"}]},
            }
        ]
        claude_events = "\n".join([events[0], json.dumps(assistant)])
        agents = self.repo / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "custom.md").write_text("agent", encoding="utf-8")

        with_flag = self.run_ralph(
            "--unsafe-allow-agents",
            "--iterations",
            "2",
            backend="claude",
            env={"FAKE_CLAUDE_EVENTS": claude_events},
        )
        self.assertEqual(with_flag.returncode, 2)
        # Both the resume and the run command reproduce the flag so recovery
        # re-establishes the same relaxed boundary.
        self.assertIn("ralph resume --backend claude", with_flag.stderr)
        resume_line = next(
            line for line in with_flag.stderr.splitlines() if "manual resume:" in line
        )
        self.assertIn("--unsafe-allow-agents", resume_line)
        # --session must remain the final argument of the resume command.
        self.assertTrue(resume_line.rstrip().endswith("--session claude-session-1"))
        continue_line = next(
            line for line in with_flag.stderr.splitlines() if "continue Ralph:" in line
        )
        self.assertIn("--unsafe-allow-agents", continue_line)

        for path in self.calls.iterdir():
            path.unlink()

        # Without the flag, neither command mentions it. (Move the agents dir
        # aside so the no-flag run is not refused before it can hand off.)
        (agents / "custom.md").unlink()
        agents.rmdir()
        without_flag = self.run_ralph(
            "--iterations",
            "2",
            backend="claude",
            env={"FAKE_CLAUDE_EVENTS": claude_events},
        )
        self.assertEqual(without_flag.returncode, 2)
        self.assertIn("manual resume:", without_flag.stderr)
        self.assertNotIn("--unsafe-allow-agents", without_flag.stderr)

    def test_claude_oauth_token_is_preserved_but_api_credentials_are_rejected(self) -> None:
        token_result = self.run_ralph(
            backend="claude",
            env={"CLAUDE_CODE_OAUTH_TOKEN": "subscription-token"},
        )
        self.assertEqual(token_result.returncode, 0, token_result.stderr)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN=subscription-token", (self.calls / "claude-env").read_text())

        for path in self.calls.iterdir():
            path.unlink()
        api_result = self.run_ralph(backend="claude", env={"ANTHROPIC_AUTH_TOKEN": "paid-token"})
        self.assertNotEqual(api_result.returncode, 0)
        self.assertIn("API credential", api_result.stderr)
        self.assertNotIn("paid-token", api_result.stdout + api_result.stderr)

        for name in (
            "ANTHROPIC_AWS_BASE_URL",
            "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
            "ANTHROPIC_CUSTOM_HEADERS",
            "ANTHROPIC_FOUNDRY_API_KEY",
            "AWS_BEARER_TOKEN_BEDROCK",
            "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
            "CLAUDE_CODE_SKIP_VERTEX_AUTH",
            "CLAUDE_CODE_USE_ANTHROPIC_AWS",
            "CLAUDE_CODE_USE_MANTLE",
        ):
            with self.subTest(name=name):
                unsafe = self.run_ralph(backend="claude", env={name: "unsafe-routing"})
                self.assertNotEqual(unsafe.returncode, 0)
                self.assertIn("API credential", unsafe.stderr)

    def test_claude_rejects_cached_server_managed_settings(self) -> None:
        managed = self.base / ".claude" / "remote-settings.json"
        managed.parent.mkdir()
        managed.write_text(json.dumps({"hooks": {}}), encoding="utf-8")

        result = self.run_ralph(backend="claude", env={"HOME": str(self.base)})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("server-managed Claude settings", result.stderr)

    def test_claude_rejects_managed_configuration_directory(self) -> None:
        # The managed-root check is host-isolated through a seam; confirm the seam
        # still fires (it is not a silent bypass) when managed config is present.
        self.managed_root.mkdir()
        (self.managed_root / "managed-settings.json").write_text("{}", encoding="utf-8")

        result = self.run_ralph(backend="claude")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("managed Claude configuration", result.stderr)

    def test_claude_rejects_managed_configuration_profiles(self) -> None:
        # A configuration profile that manages Claude Code must stop the run. The
        # profiles tool is host-isolated through a seam, so drive it with a fake
        # that reports a managing profile and confirm the check still fires.
        self._script(
            "profiles",
            """
            printf '%s\\n' "$*" >> "$FAKE_CALLS/profiles"
            printf '%s\\n' 'com.anthropic.claudecode'
            """,
        )

        result = self.run_ralph(backend="claude")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("managed Claude preferences", result.stderr)

    def test_claude_fails_closed_on_runtime_customization_and_backend_contract_errors(self) -> None:
        event_lines = self._claude_events("Done").splitlines()
        init = json.loads(event_lines[0])
        init["plugins"] = [{"name": "external-plugin"}]
        event_lines[0] = json.dumps(init)
        customized = self.run_ralph(
            backend="claude", env={"FAKE_CLAUDE_EVENTS": "\n".join(event_lines)}
        )
        self.assertNotEqual(customized.returncode, 0)
        self.assertIn("external MCP servers or plugins", customized.stderr)
        run_dir = next((self.repo / ".git" / "ralph" / "runs").iterdir())
        self.assertEqual(
            json.loads((run_dir / "session.json").read_text())["session_id"],
            "claude-session-1",
        )

        for path in self.calls.iterdir():
            path.unlink()
        missing_result = self.run_ralph(
            backend="claude",
            env={"FAKE_CLAUDE_EVENTS": "\n".join(self._claude_events("Done").splitlines()[:-1])},
        )
        self.assertNotEqual(missing_result.returncode, 0)
        self.assertIn("omitted required session metadata or final result", missing_result.stderr)

        for path in self.calls.iterdir():
            path.unlink()
        failed = self.run_ralph(backend="claude", env={"FAKE_CLAUDE_EXIT": "1"})
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("Claude session failed", failed.stderr)

    def test_real_claude_init_contract_is_accepted(self) -> None:
        # Mirror the init event a real subscription Claude Code 2.1.211 session
        # emits: `apiKeySource` is "none" (no metered API key; billing rides the
        # OAuth login preflight proved), the tools list is the full built-in
        # harness set, and unknown informational fields are present. Regression
        # for a live run refused with "did not use subscription OAuth" because
        # the parser demanded a fictional "oauth" value the CLI never reports.
        event_lines = self._claude_events("<promise>COMPLETE</promise>").splitlines()
        init = json.loads(event_lines[0])
        init["tools"] = [
            "Task", "Bash", "CronCreate", "CronDelete", "CronList", "DesignSync",
            "Edit", "EnterWorktree", "ExitWorktree", "Monitor", "NotebookEdit",
            "PushNotification", "Read", "RemoteTrigger", "ReportFindings",
            "ScheduleWakeup", "SendMessage", "Skill", "TaskCreate", "TaskGet",
            "TaskList", "TaskOutput", "TaskStop", "TaskUpdate", "ToolSearch",
            "WebFetch", "WebSearch", "Workflow", "Write",
        ]
        init["slash_commands"] = ["init", "review"]
        init["agents"] = ["claude", "Explore", "general-purpose"]
        init["capabilities"] = ["interrupt_receipt_v1"]
        init["claude_code_version"] = "2.1.211"
        event_lines[0] = json.dumps(init)
        result = self.run_ralph(
            backend="claude", env={"FAKE_CLAUDE_EVENTS": "\n".join(event_lines)}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.calls / "claude").exists())

    def test_api_key_sourced_claude_session_fails_closed(self) -> None:
        # Any reported API-key source means the session is metered, not
        # subscription OAuth; every such value must stop the run. "oauth" is
        # included because no real CLI reports it: an unexpected value must
        # fail closed rather than be mistaken for a subscription proof.
        for source in ("ANTHROPIC_API_KEY", "apiKeyHelper", "oauth", "", None):
            with self.subTest(apiKeySource=source):
                for path in self.calls.iterdir():
                    path.unlink()
                event_lines = self._claude_events("Done").splitlines()
                init = json.loads(event_lines[0])
                if source is None:
                    init.pop("apiKeySource", None)
                else:
                    init["apiKeySource"] = source
                event_lines[0] = json.dumps(init)
                result = self.run_ralph(
                    backend="claude", env={"FAKE_CLAUDE_EVENTS": "\n".join(event_lines)}
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("did not use subscription OAuth", result.stderr)
