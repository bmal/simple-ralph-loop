"""Sandbox profile template: the write allow-list and read deny-list it
interpolates, backend-aware store handling, and a real Seatbelt smoke test."""

from __future__ import annotations

import functools
from pathlib import Path
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ralph import launch  # noqa: E402  (import after sys.path is extended)
from ralph.errors import RalphError  # noqa: E402


class SandboxProfileTest(unittest.TestCase):
    """The pure Seatbelt profile generator (register D3/D4/D10) and the
    absolute-path `sandbox-exec` resolver. These assert external behavior —
    profile policy text and the resolved launcher — with no host state."""

    def setUp(self) -> None:
        self.home = Path("/Users/tester")
        self.worktree = Path("/work/project")
        self.ralph_dir = Path("/work/project/.git/ralph")
        self.session_tmp = Path("/private/var/session-tmp")

    def _opencode_profile(self) -> str:
        return launch.build_sandbox_profile(
            "opencode", self.worktree, self.ralph_dir, self.session_tmp, self.home
        )

    def _deny_read_lines(self, profile: str) -> list[str]:
        return [
            line for line in profile.splitlines() if line.strip().startswith("(deny file-read*")
        ]

    def _write_allow_lines(self, profile: str) -> list[str]:
        return [
            line for line in profile.splitlines() if line.strip().startswith("(allow file-write*")
        ]

    def test_profile_leaves_reads_permissive_and_network_open_by_default(self) -> None:
        # Reads are a deny-list and the network is unrestricted (D4/D5): the
        # policy opens with a broad default and narrows only writes and the
        # famous read paths, never egress.
        profile = self._opencode_profile()
        self.assertIn("(allow default)", profile)
        self.assertNotIn("file-write*", profile.split("(deny file-write*)")[0])

    def test_opencode_profile_denies_every_famous_credential_read(self) -> None:
        profile = self._opencode_profile()
        denied = "\n".join(self._deny_read_lines(profile))
        for relative in (
            ".ssh",
            ".gnupg",
            ".aws",
            ".config/gcloud",
            ".azure",
            ".kube",
            ".netrc",
            ".docker/config.json",
            ".npmrc",
            ".pypirc",
        ):
            self.assertIn(str(self.home / relative), denied, relative)
        # Browser profiles under Application Support are denied (famous paths,
        # not a completeness guarantee).
        self.assertIn(str(self.home / "Library" / "Application Support" / "Google" / "Chrome"), denied)
        self.assertIn(str(self.home / "Library" / "Application Support" / "Firefox"), denied)

    def test_opencode_profile_denies_keychains_but_permits_the_login_keychain(self) -> None:
        # Owner decision (2026-07-17, amends D4): the login keychain database
        # stays readable because gh's in-scope GitHub token lives there on a
        # default macOS install; every other keychain stays denied.
        profile = self._opencode_profile()
        self.assertIn(
            f'(deny file-read* (subpath "{self.home}/Library/Keychains"))', profile
        )
        self.assertIn(
            f'(allow file-read* (literal "{self.home}/Library/Keychains/login.keychain-db"))',
            profile,
        )
        # The allow-back must come after the deny so it wins (Seatbelt: last
        # matching rule).
        self.assertLess(
            profile.index('(deny file-read* (subpath "%s/Library/Keychains"))' % self.home),
            profile.index("login.keychain-db"),
        )

    def test_opencode_profile_denies_the_out_of_scope_claude_store(self) -> None:
        profile = self._opencode_profile()
        self.assertIn(f'(deny file-read* (subpath "{self.home}/.claude"))', profile)

    def test_opencode_profile_keeps_gh_and_its_own_store_readable(self) -> None:
        # In-scope credentials the loop needs (D4): gh's config and the running
        # backend's own store are never denied, so they fall through to the
        # permissive read default.
        profile = self._opencode_profile()
        denied = "\n".join(self._deny_read_lines(profile))
        self.assertNotIn(str(self.home / ".config" / "gh"), denied)
        self.assertNotIn(str(self.home / ".local" / "share" / "opencode"), denied)

    def test_write_allow_list_is_exactly_the_four_sanctioned_roots(self) -> None:
        profile = self._opencode_profile()
        self.assertIn("(deny file-write*)", profile)
        allowed = "\n".join(self._write_allow_lines(profile))
        for root in (
            self.worktree,
            self.ralph_dir,
            self.session_tmp,
            self.home / ".local" / "share" / "opencode",
        ):
            self.assertIn(f'(subpath "{root}")', allowed, str(root))
        # An out-of-worktree path is not among the sanctioned write roots.
        self.assertNotIn(str(self.home / "Documents"), allowed)
        self.assertNotIn('(subpath "/")', allowed)

    def test_claude_run_flips_the_backend_aware_store_and_deny(self) -> None:
        # Regression anchor for #22: the same generator, backend-aware. For a
        # Claude run the in-scope store is ~/.claude (readable + write root) and
        # the out-of-scope store denied is the OpenCode auth file.
        profile = launch.build_sandbox_profile(
            "claude", self.worktree, self.ralph_dir, self.session_tmp, self.home
        )
        allowed = "\n".join(self._write_allow_lines(profile))
        self.assertIn(f'(subpath "{self.home}/.claude")', allowed)
        denied = "\n".join(self._deny_read_lines(profile))
        self.assertIn(
            f'(literal "{self.home}/.local/share/opencode/auth.json")', denied
        )
        self.assertNotIn(f'(subpath "{self.home}/.claude")', denied)

    def test_profile_interpolates_only_paths_and_never_a_secret(self) -> None:
        # The generator's only inputs are paths and the backend name; it never
        # consults the environment, so no token can be interpolated (D10). Prove
        # it by planting secrets in the environment and generating.
        secret = "sk-super-secret-token-value"
        with _patched_environ(
            {
                "ANTHROPIC_API_KEY": secret,
                "GH_TOKEN": secret,
                "OPENAI_API_KEY": secret,
            }
        ):
            profile = self._opencode_profile()
        self.assertNotIn(secret, profile)
        # Every home path present is a sanctioned, hard-coded policy path — no
        # operator-specific value leaks in beyond the paths we interpolate.
        self.assertNotIn("token", profile.lower())

    def test_profile_quotes_paths_with_spaces_and_metacharacters(self) -> None:
        worktree = Path('/work/pro"ject dir')
        profile = launch.build_sandbox_profile(
            "opencode", worktree, self.ralph_dir, self.session_tmp, self.home
        )
        # The double quote inside the path is backslash-escaped so the profile
        # stays parseable rather than terminating the string early.
        self.assertIn('(subpath "/work/pro\\"ject dir")', profile)

    def test_sandbox_exec_resolves_absolute_and_honors_override(self) -> None:
        with _patched_environ({}, remove=("RALPH_SANDBOX_EXEC",)):
            self.assertEqual(launch.sandbox_exec_executable(), "/usr/bin/sandbox-exec")
        with _patched_environ({"RALPH_SANDBOX_EXEC": "/tmp/fake-sandbox-exec"}):
            self.assertEqual(launch.sandbox_exec_executable(), "/tmp/fake-sandbox-exec")


class SandboxSelfTestDecisionTest(unittest.TestCase):
    """The self-test decision (register D8), driven through the injectable probe
    runner so it is deterministic and depends on no host state. It proceeds only
    when both a denied read and a denied write are observed to fail, and stops
    fail-closed on any other outcome."""

    PROFILE = Path("/work/project/.git/ralph/runs/run-1/sandbox.sb")

    def _runner(self, outcomes: dict[str, str]):
        # A fake probe runner: it records the (profile, kind) it was asked about
        # and returns the canned outcome for that kind, missing kinds defaulting
        # to a blocked (good) probe.
        calls: list[tuple[Path, str]] = []

        def runner(profile: Path, kind: str) -> str:
            calls.append((profile, kind))
            return outcomes.get(kind, launch.PROBE_BLOCKED)

        return runner, calls

    def test_proceeds_only_when_both_probes_are_blocked(self) -> None:
        runner, calls = self._runner(
            {"read": launch.PROBE_BLOCKED, "write": launch.PROBE_BLOCKED}
        )
        # No exception means the run is cleared to proceed.
        launch.run_sandbox_self_test(self.PROFILE, runner=runner)
        # Exactly one read probe and one write probe, both against the profile —
        # the self-test runs once, not per probe kind more than once.
        self.assertEqual(calls, [(self.PROFILE, "read"), (self.PROFILE, "write")])

    def test_a_permitted_read_fails_closed_before_the_write_probe(self) -> None:
        runner, calls = self._runner({"read": launch.PROBE_ALLOWED})
        with self.assertRaises(RalphError) as caught:
            launch.run_sandbox_self_test(self.PROFILE, runner=runner)
        self.assertIn("failed open", str(caught.exception))
        self.assertIn("read", str(caught.exception))
        # It stops at the first fail-open probe and never reaches the write probe.
        self.assertEqual(calls, [(self.PROFILE, "read")])

    def test_a_permitted_write_fails_closed(self) -> None:
        runner, _calls = self._runner({"write": launch.PROBE_ALLOWED})
        with self.assertRaises(RalphError) as caught:
            launch.run_sandbox_self_test(self.PROFILE, runner=runner)
        self.assertIn("failed open", str(caught.exception))
        self.assertIn("write", str(caught.exception))

    def test_an_unrunnable_probe_fails_closed(self) -> None:
        runner, _calls = self._runner({"read": launch.PROBE_UNAVAILABLE})
        with self.assertRaises(RalphError) as caught:
            launch.run_sandbox_self_test(self.PROFILE, runner=runner)
        self.assertIn("could not run", str(caught.exception))

    def test_default_probe_invokes_sandbox_exec_by_absolute_path(self) -> None:
        # The real probe must launch `sandbox-exec -f <profile>` by absolute path
        # (never a PATH lookup) and interpret a non-zero exit as a blocked probe.
        captured: list[list[str]] = []

        class _Completed:
            returncode = 1

        def fake_run(argv, **_kwargs):
            captured.append(argv)
            return _Completed()

        with mock.patch.object(launch.subprocess, "run", fake_run), _patched_environ(
            {"RALPH_SANDBOX_EXEC": "/opt/sbx"}
        ):
            outcome = launch.default_sandbox_probe(
                self.PROFILE, "read", home=Path("/Users/tester")
            )
        self.assertEqual(outcome, launch.PROBE_BLOCKED)
        argv = captured[0]
        self.assertEqual(argv[:3], ["/opt/sbx", "-f", str(self.PROFILE)])
        self.assertIn("/Users/tester/Library/Keychains", " ".join(argv))

    def test_default_probe_reports_a_zero_exit_as_permitted(self) -> None:
        class _Completed:
            returncode = 0

        with mock.patch.object(launch.subprocess, "run", lambda *a, **k: _Completed()):
            outcome = launch.default_sandbox_probe(
                self.PROFILE, "read", home=Path("/Users/tester")
            )
        self.assertEqual(outcome, launch.PROBE_ALLOWED)

    def test_default_probe_reports_a_launch_failure_as_unavailable(self) -> None:
        def boom(*_a, **_k):
            raise FileNotFoundError("no sandbox-exec")

        with mock.patch.object(launch.subprocess, "run", boom):
            outcome = launch.default_sandbox_probe(
                self.PROFILE, "write", home=Path("/Users/tester")
            )
        self.assertEqual(outcome, launch.PROBE_UNAVAILABLE)

    def test_default_probe_treats_a_wedged_launcher_as_unavailable(self) -> None:
        # A sandbox-exec that hangs must fail closed, not stall the pre-loop gate.
        def hang(*_a, **_k):
            raise subprocess.TimeoutExpired(cmd="sandbox-exec", timeout=30)

        with mock.patch.object(launch.subprocess, "run", hang):
            outcome = launch.default_sandbox_probe(
                self.PROFILE, "read", home=Path("/Users/tester")
            )
        self.assertEqual(outcome, launch.PROBE_UNAVAILABLE)

    def test_read_probe_targets_an_existing_denied_directory(self) -> None:
        # The read probe is non-vacuous: it lists whichever famous credential
        # directory actually exists, so a fail-open profile is distinguishable
        # from a plain "no such file".
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            (home / ".ssh").mkdir()
            captured: list[list[str]] = []

            class _Completed:
                returncode = 1

            def fake_run(argv, **_kwargs):
                captured.append(argv)
                return _Completed()

            with mock.patch.object(launch.subprocess, "run", fake_run):
                launch.default_sandbox_probe(self.PROFILE, "read", home=home)
        self.assertIn(str(home / ".ssh"), " ".join(captured[0]))

    def test_read_probe_falls_back_to_keychains_when_none_exist(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)  # empty: no famous credential directory present
            captured: list[list[str]] = []

            class _Completed:
                returncode = 1

            def fake_run(argv, **_kwargs):
                captured.append(argv)
                return _Completed()

            with mock.patch.object(launch.subprocess, "run", fake_run):
                launch.default_sandbox_probe(self.PROFILE, "read", home=home)
        self.assertIn(str(home / "Library" / "Keychains"), " ".join(captured[0]))


@unittest.skipUnless(sys.platform == "darwin", "Seatbelt is macOS-only")
class SandboxSelfTestSmokeTest(unittest.TestCase):
    """Make-or-break qualification of the real self-test against the live
    `/usr/bin/sandbox-exec` (register D8). No language model and no subscription
    spend: it proves the real probe runner clears a correct profile and stops
    fail-closed under a deliberately-open one."""

    SANDBOX_EXEC = "/usr/bin/sandbox-exec"

    def setUp(self) -> None:
        if not Path(self.SANDBOX_EXEC).is_file():
            raise AssertionError(
                "/usr/bin/sandbox-exec is required for the host-isolation self-test "
                "smoke and must not be silently skipped on macOS"
            )
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.worktree = base / "worktree"
        self.worktree.mkdir()
        self.ralph_dir = base / "ralph"
        self.ralph_dir.mkdir()
        # A dedicated session tmp under the temp base so the synthetic home — and
        # thus the write probe target under it — is genuinely outside every
        # sanctioned write root (were session tmp the real TMPDIR the temp base
        # would sit inside it and the "escape" write would be sanctioned).
        self.session_tmp = base / "session-tmp"
        self.session_tmp.mkdir()
        self.home = base / "home"
        # The read probe lists ~/Library/Keychains; create it so the probe is
        # non-vacuous — an open profile really returns its (empty) listing.
        (self.home / "Library" / "Keychains").mkdir(parents=True)
        self.probe = functools.partial(launch.default_sandbox_probe, home=self.home)
        self.correct_profile = base / "sandbox.sb"
        self.correct_profile.write_text(
            launch.build_sandbox_profile(
                "opencode", self.worktree, self.ralph_dir, self.session_tmp, self.home
            ),
            encoding="utf-8",
        )
        # A profile that parses cleanly but confines nothing — the fail-open case.
        self.open_profile = base / "open.sb"
        self.open_profile.write_text("(version 1)\n(allow default)\n", encoding="utf-8")

    def test_self_test_clears_a_correct_profile(self) -> None:
        # Both probes are refused by the real kernel, so the self-test raises
        # nothing and the run would be cleared to proceed.
        launch.run_sandbox_self_test(self.correct_profile, runner=self.probe)
        # And it left no probe file behind under the synthetic home.
        self.assertFalse((self.home / launch.SANDBOX_WRITE_PROBE).exists())

    def test_self_test_fails_closed_under_a_deliberately_open_profile(self) -> None:
        with self.assertRaises(RalphError) as caught:
            launch.run_sandbox_self_test(self.open_profile, runner=self.probe)
        self.assertIn("failed open", str(caught.exception))
        # The write probe the open profile permitted is cleaned up, not littered.
        self.assertFalse((self.home / launch.SANDBOX_WRITE_PROBE).exists())

    def test_real_probes_distinguish_blocked_from_permitted(self) -> None:
        # The runner itself, exercised directly, reports the kernel's verdict.
        self.assertEqual(self.probe(self.correct_profile, "read"), launch.PROBE_BLOCKED)
        self.assertEqual(self.probe(self.correct_profile, "write"), launch.PROBE_BLOCKED)
        self.assertEqual(self.probe(self.open_profile, "read"), launch.PROBE_ALLOWED)
        self.assertEqual(self.probe(self.open_profile, "write"), launch.PROBE_ALLOWED)
        self.assertFalse((self.home / launch.SANDBOX_WRITE_PROBE).exists())


@unittest.skipUnless(sys.platform == "darwin", "Seatbelt is macOS-only")
class SandboxRealProfileSmokeTest(unittest.TestCase):
    """Make-or-break qualification against the real `/usr/bin/sandbox-exec`
    (register D2/D7). No language model and no subscription spend: it proves a
    Go-CLI TLS operation succeeds under the generated profile while a denied read
    and an out-of-worktree write actually fail. Requires network (a public
    GitHub HTTPS handshake) exactly as the parent program's smoke specifies."""

    SANDBOX_EXEC = "/usr/bin/sandbox-exec"

    def setUp(self) -> None:
        if not Path(self.SANDBOX_EXEC).is_file():
            raise AssertionError(
                "/usr/bin/sandbox-exec is required for the host-isolation smoke "
                "and must not be silently skipped on macOS"
            )
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.worktree = base / "worktree"
        self.worktree.mkdir()
        self.ralph_dir = base / "ralph"
        self.ralph_dir.mkdir()
        self.session_tmp = Path(os.environ.get("TMPDIR") or "/tmp").resolve()
        # Generate the profile against a synthetic home carrying a real, readable
        # credential file at a denied path (~/.ssh). This keeps the smoke
        # hermetic — it never touches the operator's real ~/.ssh — while proving
        # the deny rule bites a file that genuinely exists (see the read smoke).
        self.home = base / "home"
        (self.home / ".ssh").mkdir(parents=True)
        self.credential_probe = self.home / ".ssh" / "id_probe"
        self.credential_probe.write_text("PROBE-LEAK\n", encoding="utf-8")
        self.profile = base / "sandbox.sb"
        self.profile.write_text(
            launch.build_sandbox_profile(
                "opencode", self.worktree, self.ralph_dir, self.session_tmp, self.home
            ),
            encoding="utf-8",
        )

    def _confined(self, *command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.SANDBOX_EXEC, "-f", str(self.profile), *command],
            text=True,
            capture_output=True,
        )

    def test_go_cli_tls_handshake_succeeds_under_the_profile(self) -> None:
        if shutil.which("git") is None:
            raise AssertionError("git is required for the TLS smoke")
        # A public HTTPS ls-remote needs no credential and no SSH, so it isolates
        # the make-or-break question: does Go/libcurl TLS work under Seatbelt.
        result = self._confined(
            "git",
            "ls-remote",
            "https://github.com/bmal/simple-ralph-loop.git",
            "HEAD",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("HEAD", result.stdout)

    def test_denied_credential_read_actually_fails(self) -> None:
        # Non-vacuous by construction: the probe exists and is readable outside
        # the sandbox, so a profile that failed open would leak PROBE-LEAK.
        self.assertIn("PROBE-LEAK", self.credential_probe.read_text())
        result = self._confined("cat", str(self.credential_probe))
        # Under the profile the read is refused (operation not permitted), never
        # a plain "no such file": the deny rule bites a file that is really there.
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("PROBE-LEAK", result.stdout)
        self.assertIn("not permitted", (result.stdout + result.stderr).lower())

    def test_out_of_worktree_write_actually_fails(self) -> None:
        target = Path.home() / ".ralph-sandbox-escape-probe"
        self.addCleanup(lambda: target.exists() and target.unlink())
        result = self._confined(
            "sh", "-c", f'echo leak > {shlex.quote(str(target))} 2>&1; echo "rc=$?"'
        )
        self.assertNotIn("rc=0", result.stdout)
        self.assertFalse(target.exists(), "write outside the worktree was not confined")

    def test_sanctioned_worktree_write_is_permitted(self) -> None:
        # The confinement is a boundary, not a wall: writes inside the worktree —
        # the work the loop exists to do — still succeed.
        marker = self.worktree / "written-inside.txt"
        result = self._confined(
            "sh", "-c", f'echo ok > {shlex.quote(str(marker))}; echo "rc=$?"'
        )
        self.assertIn("rc=0", result.stdout)
        self.assertEqual(marker.read_text().strip(), "ok")


@unittest.skipUnless(sys.platform == "darwin", "Seatbelt is macOS-only")
class ClaudeSandboxRealProfileSmokeTest(unittest.TestCase):
    """Make-or-break qualification that the *Claude-flavored* profile — the same
    generator with the backend-aware inputs flipped (register D4/D6) — actually
    confines a Claude-shaped session under the live `/usr/bin/sandbox-exec`. No
    language model and no subscription spend: it drives plain Go-CLI / shell
    commands under the profile (the wrap Ralph builds around `claude`), proving
    the out-of-scope OpenCode store is denied, the in-scope `~/.claude` store
    stays readable, and writes are confined to the sanctioned roots.

    Nesting: Ralph never enables Claude Code's own Bash sandbox (the session runs
    with `--dangerously-skip-permissions` and CLAUDE_SETTINGS leaves the inner
    sandbox off), so there is no inner sandbox to compose with — Ralph's outer
    Seatbelt profile proven here is the sole, authoritative confinement.
    """

    SANDBOX_EXEC = "/usr/bin/sandbox-exec"

    def setUp(self) -> None:
        if not Path(self.SANDBOX_EXEC).is_file():
            raise AssertionError(
                "/usr/bin/sandbox-exec is required for the Claude host-isolation "
                "smoke and must not be silently skipped on macOS"
            )
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.worktree = base / "worktree"
        self.worktree.mkdir()
        self.ralph_dir = base / "ralph"
        self.ralph_dir.mkdir()
        # A dedicated session tmp under the temp base so the synthetic home — and
        # the write probes under it — are genuinely outside every sanctioned write
        # root (were session tmp the real TMPDIR the temp base would sit inside it
        # and the "escape" write would be sanctioned, making the probe vacuous).
        self.session_tmp = base / "session-tmp"
        self.session_tmp.mkdir()
        self.home = base / "home"
        # The out-of-scope OpenCode store carries a leak marker so a fail-open
        # deny would surface it; the in-scope ~/.claude store carries an in-scope
        # marker that must stay readable (D4, backend-aware for a Claude run).
        opencode_auth = self.home / ".local" / "share" / "opencode"
        opencode_auth.mkdir(parents=True)
        self.opencode_probe = opencode_auth / "auth.json"
        self.opencode_probe.write_text("OPENCODE-LEAK\n", encoding="utf-8")
        claude_store = self.home / ".claude"
        claude_store.mkdir(parents=True)
        self.claude_probe = claude_store / "in-scope.json"
        self.claude_probe.write_text("CLAUDE-IN-SCOPE\n", encoding="utf-8")
        self.profile = base / "sandbox.sb"
        self.profile.write_text(
            launch.build_sandbox_profile(
                "claude", self.worktree, self.ralph_dir, self.session_tmp, self.home
            ),
            encoding="utf-8",
        )

    def _confined(self, *command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.SANDBOX_EXEC, "-f", str(self.profile), *command],
            text=True,
            capture_output=True,
        )

    def test_out_of_scope_opencode_store_read_actually_fails(self) -> None:
        # Non-vacuous: the file exists and is readable outside the sandbox, so a
        # fail-open profile would leak OPENCODE-LEAK.
        self.assertIn("OPENCODE-LEAK", self.opencode_probe.read_text())
        result = self._confined("cat", str(self.opencode_probe))
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("OPENCODE-LEAK", result.stdout)
        self.assertIn("not permitted", (result.stdout + result.stderr).lower())

    def test_in_scope_claude_store_stays_readable(self) -> None:
        # The running backend's own store is in-scope (the loop needs it), so the
        # Claude-flavored profile must leave ~/.claude readable.
        result = self._confined("cat", str(self.claude_probe))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CLAUDE-IN-SCOPE", result.stdout)

    def test_out_of_worktree_write_actually_fails(self) -> None:
        target = self.home / ".ralph-claude-escape-probe"
        result = self._confined(
            "sh", "-c", f'echo leak > {shlex.quote(str(target))} 2>&1; echo "rc=$?"'
        )
        self.assertNotIn("rc=0", result.stdout)
        self.assertFalse(target.exists(), "write outside the worktree was not confined")

    def test_claude_store_write_is_permitted(self) -> None:
        # ~/.claude is the backend state root for a Claude run, so writes into it
        # (the backend's own session state) are sanctioned.
        marker = self.home / ".claude" / "written-inside.json"
        result = self._confined(
            "sh", "-c", f'echo ok > {shlex.quote(str(marker))}; echo "rc=$?"'
        )
        self.assertIn("rc=0", result.stdout)
        self.assertEqual(marker.read_text().strip(), "ok")


class _patched_environ:
    """Minimal context manager to set/remove environment variables for a
    deterministic pure-function assertion."""

    def __init__(self, values: dict[str, str], remove: tuple[str, ...] = ()) -> None:
        self._values = values
        self._remove = remove
        self._saved: dict[str, str | None] = {}

    def __enter__(self) -> "_patched_environ":
        for key, value in self._values.items():
            self._saved[key] = os.environ.get(key)
            os.environ[key] = value
        for key in self._remove:
            self._saved[key] = os.environ.get(key)
            os.environ.pop(key, None)
        return self

    def __exit__(self, *exc: object) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
