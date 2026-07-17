"""Sandbox profile template: the write allow-list and read deny-list it
interpolates, backend-aware store handling, and a real Seatbelt smoke test."""

from __future__ import annotations

from pathlib import Path
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ralph import launch  # noqa: E402  (import after sys.path is extended)


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
