"""Backend-agnostic trust-boundary preflight and prompt/model validation."""

from __future__ import annotations

import json

from harness import RalphCliTestCase


class PreflightTest(RalphCliTestCase):
    def test_preflight_rejects_api_auth_without_starting_session_or_leaking_secret(self) -> None:
        secret = "sk-secret-value"
        result = self.run_ralph(env={"OPENAI_API_KEY": secret})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("API credential", result.stderr)
        self.assertNotIn(secret, result.stdout + result.stderr)
        opencode_calls = self.calls / "opencode"
        self.assertFalse(opencode_calls.exists() and " run " in opencode_calls.read_text())

    def test_preflight_rejects_unsafe_effective_config_and_model_mismatch(self) -> None:
        unsafe = json.loads(self._config())
        unsafe["provider"]["openai"]["options"]["baseURL"] = "https://proxy.invalid"
        config_result = self.run_ralph(env={"FAKE_CONFIG": json.dumps(unsafe)})
        self.assertNotEqual(config_result.returncode, 0)
        self.assertIn("effective OpenCode configuration", config_result.stderr)

        for path in self.calls.iterdir():
            path.unlink()
        mismatch_result = self.run_ralph(
            env={"FAKE_EVENTS": self._events("Done"), "FAKE_EXPORT": self._export("Done", "gpt-other")}
        )
        self.assertNotEqual(mismatch_result.returncode, 0)
        self.assertIn("initial model", mismatch_result.stderr)

    def test_prompt_and_model_validation_happen_before_session(self) -> None:
        self.prompt.write_bytes(b"\xff")
        invalid_prompt = self.run_ralph()
        self.assertNotEqual(invalid_prompt.returncode, 0)
        self.assertIn("UTF-8", invalid_prompt.stderr)

        self.prompt.write_text("work", encoding="utf-8")
        invalid_model = self.run_ralph("--model", "anthropic/claude")
        self.assertNotEqual(invalid_model.returncode, 0)
        self.assertIn("openai/", invalid_model.stderr)

    def test_preflight_rejects_backend_and_github_failures(self) -> None:
        cases = [
            ({"FAKE_VERSION": "1.17.19"}, "1.17.20"),
            ({"FAKE_MODELS": "openai/gpt-other"}, "unavailable"),
            ({"FAKE_AUTH": "OpenAI oauth\nAnthropic api"}, "OpenAI OAuth"),
            ({"FAKE_GH_FAIL": "1"}, "gh preflight"),
        ]
        for environment, message in cases:
            with self.subTest(environment=environment):
                result = self.run_ralph(env=environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                for path in self.calls.iterdir():
                    path.unlink()
