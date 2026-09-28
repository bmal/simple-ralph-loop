"""Claude hook exclusion (ADR-0002): a repository's committed hooks are admitted,
switched off for the session by ``disableAllHooks``, and that switch is proven per
Iteration by a canary hook in Ralph's own settings that must never fire."""

from __future__ import annotations

import json

from harness import RalphCliTestCase

HOOKS_FIRED = "Claude ran a hook although Ralph disabled hooks"
CANARY_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "Stop")


class ClaudeHookExclusionTest(RalphCliTestCase):
    def _run_dir(self):
        runs = (self.repo / ".git" / "ralph" / "runs").iterdir()
        return max(runs, key=lambda path: path.name)

    def _commit_project_hooks(self) -> None:
        # The shape bmal/noteforge commits: a settings `hooks` key and the script
        # it names under `.claude/hooks`.
        claude_dir = self.repo / ".claude"
        (claude_dir / "hooks").mkdir(parents=True)
        (claude_dir / "hooks" / "guard.py").write_text("print('guard')\n", encoding="utf-8")
        (claude_dir / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "UserPromptSubmit": [
                            {"hooks": [{"type": "command", "command": "python3 .claude/hooks/guard.py"}]}
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

    def test_committed_project_hooks_are_admitted(self) -> None:
        self._commit_project_hooks()
        result = self.run_ralph(backend="claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Claude customizations", result.stderr)
        self.assertTrue((self.calls / "claude").exists())

    def test_iteration_disables_hooks_and_arms_an_unfired_canary(self) -> None:
        result = self.run_ralph(backend="claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        invocation = next(
            line for line in (self.calls / "claude").read_text().splitlines()
            if line.startswith("-p ")
        )
        self.assertIn('"disableAllHooks":true', invocation)
        canary = self._run_dir().resolve() / "hook-canary"
        for event in CANARY_EVENTS:
            self.assertIn(f'"{event}":[', invocation)
            self.assertIn(f"/usr/bin/touch {canary / event}", invocation)
        # Armed, and silent: hooks were off, so no canary file was left behind.
        self.assertTrue(canary.is_dir())
        self.assertEqual(list(canary.iterdir()), [])

    def test_a_hook_firing_before_the_first_init_fails_closed(self) -> None:
        # SessionStart runs before the stream: the first init's proof sees it.
        self._commit_project_hooks()
        result = self.run_ralph(backend="claude", env={"FAKE_CLAUDE_RUN_HOOKS": "before"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(HOOKS_FIRED, result.stderr)
        outcome = json.loads((self._run_dir() / "outcome.json").read_text())
        self.assertEqual(outcome["outcome"], "backend_contract_failure")
        # Refused at the init itself: Ralph read no further than that one event.
        retained = (self._run_dir() / "stdout.ndjson").read_text().splitlines()
        self.assertEqual(len(retained), 1)
        self.assertEqual(json.loads(retained[0])["subtype"], "init")

    def test_a_hook_firing_after_the_last_init_fails_closed(self) -> None:
        # A tool-use or Stop hook fires after every init has been proven; the
        # end-of-stream check still refuses the completed session.
        result = self.run_ralph(backend="claude", env={"FAKE_CLAUDE_RUN_HOOKS": "after"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(HOOKS_FIRED, result.stderr)
        outcome = json.loads((self._run_dir() / "outcome.json").read_text())
        self.assertEqual(outcome["outcome"], "backend_contract_failure")
        self.assertIn("--session", result.stderr)

    def test_resume_still_disables_hooks(self) -> None:
        # Recovery is interactive and replaces the process, so it carries the
        # base settings -- hooks off -- without a canary nobody would read.
        from ralph.backends.claude import CLAUDE_SETTINGS, resume_argv

        argv = resume_argv(self.repo, "claude-opus-5-5", "session-1")
        self.assertEqual(argv[argv.index("--settings") + 1], CLAUDE_SETTINGS)
        self.assertTrue(json.loads(CLAUDE_SETTINGS)["disableAllHooks"])
        self.assertNotIn("hooks", json.loads(CLAUDE_SETTINGS))
