"""The --unsafe-allow-agents opt-out: which vectors it relaxes, which it
never relaxes, and how the refusal advertises it."""

from __future__ import annotations

import json

from harness import RalphCliTestCase


class UnsafeAllowAgentsTest(RalphCliTestCase):
    def test_unsafe_allow_agents_relaxes_only_the_claude_agent_vectors(self) -> None:
        agents = self.repo / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "custom.md").write_text("custom agent", encoding="utf-8")

        refused = self.run_ralph(backend="claude")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("Claude customizations", refused.stderr)
        self.assertFalse((self.calls / "claude").exists())

        allowed = self.run_ralph("--unsafe-allow-agents", backend="claude")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertTrue((self.calls / "claude").exists())
        self.assertIn("--unsafe-allow-agents is set", allowed.stderr)

        for path in self.calls.iterdir():
            path.unlink()

        # The flag is scoped to agents: a co-present hooks directory is still
        # refused, and the backend is never launched.
        hooks = self.repo / ".claude" / "hooks"
        hooks.mkdir()
        with_hooks = self.run_ralph("--unsafe-allow-agents", backend="claude")
        self.assertNotEqual(with_hooks.returncode, 0)
        self.assertIn("Claude customizations", with_hooks.stderr)
        self.assertFalse((self.calls / "claude").exists())
        hooks.rmdir()

        # settings.json: the flag admits the `agent` key but not other unsafe keys.
        settings = self.repo / ".claude" / "settings.json"
        settings.write_text(json.dumps({"agent": {"reviewer": {}}}), encoding="utf-8")
        agent_key = self.run_ralph("--unsafe-allow-agents", backend="claude")
        self.assertEqual(agent_key.returncode, 0, agent_key.stderr)

        for path in self.calls.iterdir():
            path.unlink()
        settings.write_text(
            json.dumps({"agent": {"reviewer": {}}, "hooks": {}}), encoding="utf-8"
        )
        mixed = self.run_ralph("--unsafe-allow-agents", backend="claude")
        self.assertNotEqual(mixed.returncode, 0)
        self.assertIn("Claude customizations", mixed.stderr)

    def test_agent_only_refusal_advertises_the_opt_out(self) -> None:
        claude_dir = self.repo / ".claude"
        claude_dir.mkdir()
        settings = claude_dir / "settings.json"

        # An agents-directory-only refusal names the opt-out.
        agents = claude_dir / "agents"
        agents.mkdir()
        (agents / "custom.md").write_text("agent", encoding="utf-8")
        dir_only = self.run_ralph(backend="claude")
        self.assertNotEqual(dir_only.returncode, 0)
        self.assertIn("Claude customizations", dir_only.stderr)
        self.assertIn("--unsafe-allow-agents", dir_only.stderr)
        self.assertFalse((self.calls / "claude").exists())
        (agents / "custom.md").unlink()
        agents.rmdir()

        # An `agent`-key-only refusal names the opt-out too.
        settings.write_text(json.dumps({"agent": {"reviewer": {}}}), encoding="utf-8")
        key_only = self.run_ralph(backend="claude")
        self.assertNotEqual(key_only.returncode, 0)
        self.assertIn("Claude customizations", key_only.stderr)
        self.assertIn("--unsafe-allow-agents", key_only.stderr)
        settings.unlink()

    def test_non_agent_refusals_never_advertise_the_opt_out(self) -> None:
        claude_dir = self.repo / ".claude"
        claude_dir.mkdir()
        settings = claude_dir / "settings.json"
        agents = claude_dir / "agents"

        def _refusal(*, agents_present: bool, dir_name: str | None, keys: dict) -> str:
            if agents_present:
                agents.mkdir()
            other_dir = claude_dir / dir_name if dir_name else None
            if other_dir is not None:
                other_dir.mkdir()
            if keys:
                settings.write_text(json.dumps(keys), encoding="utf-8")
            result = self.run_ralph(backend="claude")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Claude customizations", result.stderr)
            # The opt-out must never be dangled when the flag cannot relax the
            # blocker; setting it would be a false remedy.
            self.assertNotIn("--unsafe-allow-agents", result.stderr)
            self.assertFalse((self.calls / "claude").exists())
            if agents_present:
                agents.rmdir()
            if other_dir is not None:
                other_dir.rmdir()
            if settings.exists():
                settings.unlink()
            return result.stderr

        # A hooks directory and a plugins directory each stay plain.
        _refusal(agents_present=False, dir_name="hooks", keys={})
        _refusal(agents_present=False, dir_name="plugins", keys={})
        # A mixed agents+hooks layout stays plain (agents is not the sole blocker).
        _refusal(agents_present=True, dir_name="hooks", keys={})
        _refusal(agents_present=True, dir_name="plugins", keys={})
        # Another unsafe key alone stays plain.
        _refusal(agents_present=False, dir_name=None, keys={"hooks": {}})
        _refusal(agents_present=False, dir_name=None, keys={"env": {"X": "1"}})
        # `agent` alongside another unsafe key stays plain.
        _refusal(agents_present=False, dir_name=None, keys={"agent": {}, "hooks": {}})
        # The agents directory alongside a non-agent settings key stays plain.
        _refusal(agents_present=True, dir_name=None, keys={"hooks": {}})

    def test_managed_config_refusal_never_advertises_the_opt_out(self) -> None:
        # Managed configuration is refused even when an agents directory is the
        # only local customization: the flag cannot relax managed config, so the
        # refusal must stay plain and take precedence over the agent vector.
        agents = self.repo / ".claude" / "agents"
        agents.mkdir(parents=True)
        self.managed_root.mkdir()
        (self.managed_root / "managed-settings.json").write_text("{}", encoding="utf-8")

        result = self.run_ralph(backend="claude")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("managed Claude configuration", result.stderr)
        self.assertNotIn("--unsafe-allow-agents", result.stderr)
        self.assertFalse((self.calls / "claude").exists())

    def test_flag_does_not_relax_plugins_or_mixed_agents_plugins(self) -> None:
        claude_dir = self.repo / ".claude"
        plugins = claude_dir / "plugins"

        # A plugins directory is not an agent vector; the flag cannot relax it,
        # so the run stays refused, the backend never launches, and — with no
        # agents directory present — the warning stays silent and the plain
        # refusal is offered with no opt-out hint.
        plugins.mkdir(parents=True)
        plugins_only = self.run_ralph("--unsafe-allow-agents", backend="claude")
        self.assertNotEqual(plugins_only.returncode, 0)
        self.assertIn("Claude customizations", plugins_only.stderr)
        self.assertNotIn("--unsafe-allow-agents", plugins_only.stderr)
        self.assertFalse((self.calls / "claude").exists())

        for path in self.calls.iterdir():
            path.unlink()

        # A mixed agents+plugins layout stays refused: agents is admitted (so the
        # isolation warning fires) but the plugins directory remains a blocker,
        # and the refusal carries no opt-out hint because agents is not the sole
        # blocker.
        agents = claude_dir / "agents"
        agents.mkdir()
        (agents / "custom.md").write_text("agent", encoding="utf-8")
        mixed = self.run_ralph("--unsafe-allow-agents", backend="claude")
        self.assertNotEqual(mixed.returncode, 0)
        self.assertIn("Claude customizations", mixed.stderr)
        self.assertNotIn("the only blocker", mixed.stderr)
        self.assertFalse((self.calls / "claude").exists())

    def test_flag_does_not_relax_managed_config_or_other_settings_keys(self) -> None:
        claude_dir = self.repo / ".claude"
        claude_dir.mkdir()
        settings = claude_dir / "settings.json"

        # Managed configuration is not an agent vector; the flag cannot relax it,
        # so it stays refused with the plain managed-config message and no hint.
        self.managed_root.mkdir()
        (self.managed_root / "managed-settings.json").write_text("{}", encoding="utf-8")
        managed = self.run_ralph("--unsafe-allow-agents", backend="claude")
        self.assertNotEqual(managed.returncode, 0)
        self.assertIn("managed Claude configuration", managed.stderr)
        self.assertNotIn("--unsafe-allow-agents", managed.stderr)
        self.assertFalse((self.calls / "claude").exists())
        (self.managed_root / "managed-settings.json").unlink()
        self.managed_root.rmdir()

        for path in self.calls.iterdir():
            path.unlink()

        # The flag admits the `agent` key alone, but each other unsafe settings
        # key co-present with it remains a blocker: the flag relaxes exactly
        # `agent` and nothing else, so every such run is refused without launch.
        other_keys = (
            "apiKeyHelper",
            "awsAuthRefresh",
            "awsCredentialExport",
            "enabledPlugins",
            "env",
            "extraKnownMarketplaces",
            "hooks",
        )
        for key in other_keys:
            with self.subTest(key=key):
                settings.write_text(
                    json.dumps({"agent": {"reviewer": {}}, key: {}}), encoding="utf-8"
                )
                result = self.run_ralph("--unsafe-allow-agents", backend="claude")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Claude customizations", result.stderr)
                # No agents *directory* is present, so the warning is silent and
                # the flag string must not appear anywhere in the output.
                self.assertNotIn("--unsafe-allow-agents", result.stderr)
                self.assertFalse((self.calls / "claude").exists())
                for path in self.calls.iterdir():
                    path.unlink()

    def test_flag_warning_fires_only_with_an_agents_directory(self) -> None:
        warning = "Ralph is not proving Claude subagent isolation"
        claude_dir = self.repo / ".claude"

        # Flag + agents directory: the warning fires and the run proceeds.
        agents = claude_dir / "agents"
        agents.mkdir(parents=True)
        (agents / "custom.md").write_text("agent", encoding="utf-8")
        with_dir = self.run_ralph("--unsafe-allow-agents", backend="claude")
        self.assertEqual(with_dir.returncode, 0, with_dir.stderr)
        self.assertIn(warning, with_dir.stderr)
        (agents / "custom.md").unlink()
        agents.rmdir()

        for path in self.calls.iterdir():
            path.unlink()

        # Flag set with the `agent` key but no agents directory: the key is
        # admitted, the run proceeds, and the warning stays silent because it
        # keys on the directory, not the settings key.
        settings = claude_dir / "settings.json"
        settings.write_text(json.dumps({"agent": {"reviewer": {}}}), encoding="utf-8")
        key_only = self.run_ralph("--unsafe-allow-agents", backend="claude")
        self.assertEqual(key_only.returncode, 0, key_only.stderr)
        self.assertNotIn(warning, key_only.stderr)
        self.assertNotIn("--unsafe-allow-agents", key_only.stderr)
        settings.unlink()
        claude_dir.rmdir()

        for path in self.calls.iterdir():
            path.unlink()

        # Flag set with no .claude customizations at all: warning stays silent.
        clean_with_flag = self.run_ralph("--unsafe-allow-agents", backend="claude")
        self.assertEqual(clean_with_flag.returncode, 0, clean_with_flag.stderr)
        self.assertNotIn(warning, clean_with_flag.stderr)

        for path in self.calls.iterdir():
            path.unlink()

        # Flag absent on an ordinary run: warning stays silent.
        without_flag = self.run_ralph(backend="claude")
        self.assertEqual(without_flag.returncode, 0, without_flag.stderr)
        self.assertNotIn(warning, without_flag.stderr)

    def test_flag_is_preflight_only_for_runtime_isolation_and_launch_flags(self) -> None:
        agents = self.repo / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "custom.md").write_text("agent", encoding="utf-8")

        def _launch_line() -> str:
            lines = [
                line
                for line in (self.calls / "claude").read_text().splitlines()
                if line.startswith("-p ")
            ]
            self.assertEqual(len(lines), 1)
            return lines[0]

        # The flag is a preflight relaxation only: the launch argv is identical
        # to a run without it and never carries the flag itself.
        allowed = self.run_ralph("--unsafe-allow-agents", backend="claude")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        flagged_launch = _launch_line()
        self.assertNotIn("--unsafe-allow-agents", flagged_launch)
        self.assertIn("--strict-mcp-config", flagged_launch)
        self.assertIn("--setting-sources project", flagged_launch)
        self.assertIn("--dangerously-skip-permissions", flagged_launch)

        (agents / "custom.md").unlink()
        agents.rmdir()
        for path in self.calls.iterdir():
            path.unlink()

        plain = self.run_ralph(backend="claude")
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertEqual(flagged_launch, _launch_line())

        # The flag never relaxes the runtime init-event isolation assertions:
        # even with agents admitted at preflight, an init event that loads
        # external MCP servers, plugins, or an unknown tool still fails closed.
        agents.mkdir()
        (agents / "custom.md").write_text("agent", encoding="utf-8")
        isolation_cases = (
            ({"plugins": [{"name": "external"}]}, "external MCP servers or plugins"),
            ({"mcp_servers": [{"name": "external"}]}, "external MCP servers or plugins"),
            ({"tools": ["Bash", "Read", "MysteryTool"]}, "unknown or external tool"),
        )
        for mutation, message in isolation_cases:
            with self.subTest(mutation=tuple(mutation)):
                for path in self.calls.iterdir():
                    path.unlink()
                event_lines = self._claude_events("Done").splitlines()
                init = json.loads(event_lines[0])
                init.update(mutation)
                event_lines[0] = json.dumps(init)
                result = self.run_ralph(
                    "--unsafe-allow-agents",
                    backend="claude",
                    env={"FAKE_CLAUDE_EVENTS": "\n".join(event_lines)},
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                # Preflight admitted the agents vector, so the failure is the
                # runtime isolation assertion — not the customization refusal —
                # which proves the flag is preflight-only.
                self.assertNotIn("Claude customizations", result.stderr)
                self.assertTrue((self.calls / "claude").exists())

    def test_agents_repository_resumes_under_the_flag_without_refusal(self) -> None:
        agents = self.repo / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "custom.md").write_text("agent", encoding="utf-8")

        # Without the flag, resuming an agents repository is refused before the
        # backend is relaunched.
        refused = self.resume_ralph("claude", "claude-opus-4-8", "claude-session-1")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("Claude customizations", refused.stderr)
        self.assertFalse((self.calls / "claude-resume").exists())

        for path in self.calls.iterdir():
            path.unlink()

        # With the flag, the same repository resumes: preflight admits the agents
        # vector (with the isolation warning) and the sanitized backend
        # relaunches. The relaunch argv re-establishes the trust boundary and
        # never carries the flag itself.
        allowed = self.resume_ralph(
            "claude",
            "claude-opus-4-8",
            "claude-session-1",
            "--unsafe-allow-agents",
        )
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertIn("Ralph is not proving Claude subagent isolation", allowed.stderr)
        resume_call = (self.calls / "claude-resume").read_text()
        self.assertIn("--resume claude-session-1", resume_call)
        self.assertNotIn("--unsafe-allow-agents", resume_call)
