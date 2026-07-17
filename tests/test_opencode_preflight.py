"""OpenCode adapter preflight: auth-output contract, route validation and
fallback recording, and agent-map handling under the opt-out flag."""

from __future__ import annotations

import json

from harness import RalphCliTestCase


class OpencodePreflightTest(RalphCliTestCase):
    def test_opencode_auth_output_contract_is_strict(self) -> None:
        supported = "┌  Credentials ~/.local/share/opencode/auth.json\n│\n●  OpenAI oauth\n│\n└  1 credentials"
        accepted = self.run_ralph(env={"FAKE_AUTH": supported})
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

        for auth in (
            "OpenAI OAuth token",
            "┌ Credentials path\n│\n● OpenAI oauth\n● Unknown credential\n│\n└ 2 credentials",
            "┌ Credentials ~/.local/share/opencode/auth.json\n│\n● OpenAI oauth\n│\n└ 2 credentials",
        ):
            with self.subTest(auth=auth):
                for path in self.calls.iterdir():
                    path.unlink()
                rejected = self.run_ralph(env={"FAKE_AUTH": auth})
                self.assertEqual(rejected.returncode, 2)
                self.assertIn("unfamiliar or ambiguous", rejected.stderr)
                calls = (self.calls / "opencode").read_text()
                self.assertNotIn(" run ", calls)

    def test_opencode_validates_every_exported_assistant_route_and_records_fallback(self) -> None:
        alternate = self._export_messages(
            "Done",
            [("openai", "gpt-5.6-sol"), ("anthropic", "claude-opus-4-8")],
        )
        rejected = self.run_ralph(env={"FAKE_EXPORT": alternate})
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("session export omitted required metadata", rejected.stderr)

        for path in self.calls.iterdir():
            path.unlink()
        fallback_export = self._export_messages(
            "Implemented.",
            [("openai", "gpt-5.6-sol"), ("openai", "gpt-5.5-codex")],
        )
        fallback = self.run_ralph(
            env={
                "FAKE_EVENTS": self._events("Implemented."),
                "FAKE_EXPORT": fallback_export,
            }
        )
        self.assertEqual(fallback.returncode, 1, fallback.stderr)
        runs = sorted((self.repo / ".git" / "ralph" / "runs").iterdir())
        session = json.loads((runs[-1] / "session.json").read_text())
        self.assertEqual(
            session["ralph_verification"]["fallback_models"],
            ["openai/gpt-5.5-codex"],
        )

    def test_opencode_rejects_later_streamed_provider_substitution(self) -> None:
        events = [
            {
                "type": "message.updated",
                "properties": {
                    "info": {
                        "id": "msg_1",
                        "sessionID": "ses_1",
                        "role": "assistant",
                        "providerID": "openai",
                        "modelID": "gpt-5.6-sol",
                    }
                },
            },
            {
                "type": "message.updated",
                "properties": {
                    "info": {
                        "id": "msg_2",
                        "sessionID": "ses_1",
                        "role": "assistant",
                        "providerID": "anthropic",
                        "modelID": "claude-opus-4-8",
                    }
                },
            },
        ]
        result = self.run_ralph(env={"FAKE_EVENTS": "\n".join(map(json.dumps, events))})
        self.assertEqual(result.returncode, 2)
        self.assertIn("alternate or malformed provider route", result.stderr)
        self.assertIn("--session ses_1", result.stderr)

        for path in self.calls.iterdir():
            path.unlink()
        missing_session = events[0].copy()
        missing_session["properties"] = {"info": dict(events[0]["properties"]["info"])}
        del missing_session["properties"]["info"]["sessionID"]
        missing = self.run_ralph(env={"FAKE_EVENTS": json.dumps(missing_session)})
        self.assertEqual(missing.returncode, 2)
        self.assertIn("omitted routing metadata", missing.stderr)
        run_dirs = sorted((self.repo / ".git" / "ralph" / "runs").iterdir())
        outcome = json.loads((run_dirs[-1] / "outcome.json").read_text())
        self.assertEqual(outcome["outcome"], "backend_contract_failure")
        self.assertEqual(len(outcome["iterations"]), 1)

    def test_opencode_agents_are_refused_without_the_flag_and_admitted_with_it(self) -> None:
        # OpenCode loads project and global agents even under --pure, and they
        # all surface in the effective configuration's agent map, so a
        # non-empty map is refused before any session starts — and the refusal
        # advertises the opt-out, because the agent check runs after every
        # other preflight proof and is by construction the sole blocker.
        agents_config = self._config(agents={"reviewer": {"name": "reviewer"}})
        refused = self.run_ralph(env={"FAKE_CONFIG": agents_config})
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("OpenCode agents must be disabled", refused.stderr)
        self.assertIn("--unsafe-allow-agents", refused.stderr)
        self.assertFalse((self.calls / "stdin").exists())

        for path in self.calls.iterdir():
            path.unlink()

        # With the flag the same configuration runs, with the isolation warning.
        allowed = self.run_ralph("--unsafe-allow-agents", env={"FAKE_CONFIG": agents_config})
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertIn("Ralph is not proving OpenCode agent isolation", allowed.stderr)
        self.assertTrue((self.calls / "stdin").exists())

        for path in self.calls.iterdir():
            path.unlink()

        # Flag set with an empty agent map: the run proceeds and the warning
        # stays silent, exactly like the Claude backend with no agents present.
        clean_with_flag = self.run_ralph("--unsafe-allow-agents")
        self.assertEqual(clean_with_flag.returncode, 0, clean_with_flag.stderr)
        self.assertNotIn("Ralph is not proving OpenCode agent isolation", clean_with_flag.stderr)

    def test_opencode_config_without_an_agent_map_fails_closed(self) -> None:
        # An effective configuration whose agent map is missing or not an
        # object is unfamiliar: Ralph cannot prove agent isolation from it, so
        # it is refused even when the flag is set.
        config = json.loads(self._config())
        del config["agent"]
        for extra in ((), ("--unsafe-allow-agents",)):
            with self.subTest(extra=extra):
                result = self.run_ralph(*extra, env={"FAKE_CONFIG": json.dumps(config)})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("omitted the agent map", result.stderr)
                self.assertFalse((self.calls / "stdin").exists())
                for path in self.calls.iterdir():
                    path.unlink()

    def test_opencode_handoff_and_resume_reproduce_the_flag(self) -> None:
        # A handed-off OpenCode run under the flag reproduces it in both the
        # resume and the continue commands, and `ralph resume` accepts it and
        # re-proves the same relaxed boundary.
        agents_config = self._config(agents={"reviewer": {"name": "reviewer"}})
        final = "<promise>NEEDS_INPUT</promise>\nWhich option should I use?"
        handoff = self.run_ralph(
            "--unsafe-allow-agents",
            "--iterations",
            "2",
            env={
                "FAKE_CONFIG": agents_config,
                "FAKE_EVENTS": self._events(final),
                "FAKE_EXPORT": self._export(final),
            },
        )
        self.assertEqual(handoff.returncode, 2)
        resume_line = next(
            line for line in handoff.stderr.splitlines() if "manual resume:" in line
        )
        self.assertIn("--unsafe-allow-agents", resume_line)
        self.assertTrue(resume_line.rstrip().endswith("--session ses_1"))
        continue_line = next(
            line for line in handoff.stderr.splitlines() if "continue Ralph:" in line
        )
        self.assertIn("--unsafe-allow-agents", continue_line)

        for path in self.calls.iterdir():
            path.unlink()

        # Without the flag, resuming the agents configuration is refused before
        # the backend relaunches.
        refused = self.resume_ralph(
            "opencode", "openai/gpt-5.6-sol", "ses_1", env={"FAKE_CONFIG": agents_config}
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("OpenCode agents must be disabled", refused.stderr)
        self.assertFalse((self.calls / "opencode-resume").exists())

        for path in self.calls.iterdir():
            path.unlink()

        # With the flag, the same configuration resumes with the isolation
        # warning, and the relaunch argv never carries the flag itself.
        allowed = self.resume_ralph(
            "opencode",
            "openai/gpt-5.6-sol",
            "ses_1",
            "--unsafe-allow-agents",
            env={"FAKE_CONFIG": agents_config},
        )
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertIn("Ralph is not proving OpenCode agent isolation", allowed.stderr)
        resume_call = (self.calls / "opencode-resume").read_text()
        self.assertIn("--session ses_1", resume_call)
        self.assertNotIn("--unsafe-allow-agents", resume_call)
