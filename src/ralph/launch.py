"""The Launch chain: the power-assertion seam, host-isolation profile and wrap,
the loop-wide ``CaffeinateAssertion``, and recovery-command formatting.

This module is the home of the Launch chain's building blocks — the ``caffeinate``
power assertion and the ``sandbox-exec`` host-isolation profile and wrap that sit,
in that order, outside every backend session — together with the loop-wide
``CaffeinateAssertion`` and the formatting of the resume/restart recovery commands.
It is the seam #19 edits to land host isolation, so the sandbox wrap and the
recovery commands that must reproduce its flags live together. ``session_argv`` is
the single point the two Backend adapters and ``cli.resume`` assemble the wrapped
argv, and ``sandbox_profile_for`` is the single place the per-run host-isolation
profile is generated — OpenCode today, with the Claude wrap landing in #22 as the
edit that drops that gate.

Invariants:
- ``caffeinate`` and ``sandbox-exec`` are always resolved by absolute path, never
  through PATH, so a repository-local or otherwise shadowed executable cannot
  silently replace the sleep assertion or the host-isolation boundary. The
  ``RALPH_CAFFEINATE`` / ``RALPH_SANDBOX_EXEC`` overrides are internal test seams
  only; production always uses the system binaries.
- ``build_sandbox_profile`` is pure: it maps already-resolved absolute paths plus
  the backend name to Seatbelt policy text and consults nothing else, so no secret
  can be interpolated (register D10) and the same inputs always yield the same
  profile. Reads are a deny-list of *famous* credential paths (not a completeness
  guarantee), writes an allow-list of the sanctioned roots, egress unrestricted;
  the deny-list is backend-aware so the other backend's auth store is denied while
  the running backend's own store stays readable (D4). The login keychain is
  allowed back after the keychain deny so the allow wins (owner-amended D4).
- The concrete filled-in profile is written only under the untracked ``.git/ralph``
  run directory (D10); tracked source holds only the universal generator.
- Recovery commands route through ``ralph resume`` / ``ralph run`` so replayed
  recovery re-establishes the full Trust boundary rather than inheriting the
  operator's ambient environment; ``--unsafe-allow-agents`` is reproduced so resume
  re-proves the same relaxed boundary, and ``--session`` is placed last.
- The loop-wide ``CaffeinateAssertion`` must cover the entire invocation: if it
  exits unexpectedly the sleep guarantee is gone, so ``ensure_alive`` fails closed
  and the loop stops at the next boundary rather than continuing unprotected.

Depends on / must not know: ``errors``. It must not know how the Loop schedules
Iterations or how a Backend consumes the argv it helps build.

See also: ``process`` (per-Iteration process control), ``loop`` (holds the
CaffeinateAssertion and prints handoffs), the Backend adapters (wrap their argv).
"""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess

from .errors import RalphError


# Absolute path to the macOS sleep-assertion tool. It is invoked by absolute
# path (never resolved through PATH) so a repository-local or otherwise
# shadowed `caffeinate` cannot silently replace the real one. RALPH_CAFFEINATE
# is an internal test seam that lets the suite substitute a fake; production
# runs never set it and always use the system binary.
DEFAULT_CAFFEINATE = "/usr/bin/caffeinate"
# Absolute path to the macOS Seatbelt launcher. Resolved by absolute path (never
# through PATH) so a shadowed `sandbox-exec` cannot silently replace the host
# isolation boundary, exactly as DEFAULT_CAFFEINATE is treated. RALPH_SANDBOX_EXEC
# is an internal test seam that lets the suite substitute a fake; production runs
# never set it and always use the system binary.
DEFAULT_SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# Famous credential paths made unreadable to a backend session (register D4).
# This is deliberately the well-known set, not a completeness guarantee: a
# credential in an unanticipated path stays readable, and the README says so.
# Directory trees denied wholesale, relative to the operator's home.
SANDBOX_DENY_READ_DIRS = (
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".kube",
    ".config/gcloud",
)
# Single credential files denied by exact path (denying a parent would also hide
# in-scope siblings such as ~/.config/gh or ~/.docker/ contexts).
SANDBOX_DENY_READ_FILES = (
    ".netrc",
    ".docker/config.json",
    ".npmrc",
    ".pypirc",
)
# Browser profile stores holding saved passwords, cookies, and sessions. Famous
# paths only, consistent with the non-exhaustive framing above.
SANDBOX_DENY_READ_BROWSER_DIRS = (
    "Library/Application Support/Google/Chrome",
    "Library/Application Support/Chromium",
    "Library/Application Support/BraveSoftware",
    "Library/Application Support/Microsoft Edge",
    "Library/Application Support/Firefox",
    "Library/Application Support/Arc",
    "Library/Application Support/Vivaldi",
    "Library/Safari",
)
# The macOS login keychain database. It is denied as part of ~/Library/Keychains
# but allowed back by exact path (register D4, amended by the owner on
# 2026-07-17): on a default macOS install gh stores its in-scope GitHub token in
# this single file, and it cannot be separated from the operator's other keychain
# secrets at the filesystem layer. The file is encrypted at rest, so an
# accidental read yields ciphertext; deliberate securityd harvesting is malice,
# which register D1 scopes out. Every other keychain stays denied.
SANDBOX_LOGIN_KEYCHAIN = "Library/Keychains/login.keychain-db"


def caffeinate_executable() -> str:
    # Always an absolute path so the sleep assertion cannot be satisfied by a
    # PATH-shadowed executable. RALPH_CAFFEINATE is honored only as a test seam.
    return os.environ.get("RALPH_CAFFEINATE") or DEFAULT_CAFFEINATE


def sandbox_exec_executable() -> str:
    # Always an absolute path so host isolation cannot be defeated by a
    # PATH-shadowed `sandbox-exec`. RALPH_SANDBOX_EXEC is honored only as a test
    # seam; production runs always use the system binary.
    return os.environ.get("RALPH_SANDBOX_EXEC") or DEFAULT_SANDBOX_EXEC


def _sandbox_quote(path: Path) -> str:
    # Seatbelt profile strings are double-quoted; backslash-escape the two
    # characters that would otherwise terminate or corrupt the string so a
    # worktree path containing a space or a quote stays parseable. Only paths are
    # ever interpolated — never a secret (register D10).
    text = str(path)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_sandbox_profile(
    backend: str,
    worktree: Path,
    ralph_dir: Path,
    session_tmp: Path,
    home: Path,
) -> str:
    # Produce the Seatbelt (`sandbox-exec`) profile text confining a backend
    # session (register D2/D3/D4/D5/D10). Pure: it maps already-resolved absolute
    # paths plus the backend name to policy text and consults nothing else, so no
    # secret can be interpolated and the same inputs always yield the same
    # profile. Reads stay permissive with a deny-list of famous credential paths;
    # writes are an allow-list of the sanctioned roots; network egress is
    # unrestricted. The deny-list is backend-aware: the *other* backend's auth
    # store is denied while the running backend's own store stays readable (D4).
    if backend == "claude":
        backend_store = home / ".claude"
        # The out-of-scope OpenCode credential is a single file; deny it exactly.
        out_of_scope_deny = f'(deny file-read* (literal {_sandbox_quote(home / ".local/share/opencode/auth.json")}))'
    else:
        backend_store = home / ".local/share/opencode"
        # The out-of-scope Claude store is a directory tree; deny it wholesale.
        out_of_scope_deny = f'(deny file-read* (subpath {_sandbox_quote(home / ".claude")}))'

    lines = [
        "(version 1)",
        ";; Ralph host isolation (register D2-D5/D10). Defends against accident,",
        ";; not malice: reads are a deny-list of famous credential paths (not a",
        ";; completeness guarantee) and network egress stays open by design.",
        "(allow default)",
        "",
        ";; Writes: allow-list of the sanctioned roots only (D3); deny all else.",
        "(deny file-write*)",
    ]
    for root in (worktree, ralph_dir, session_tmp, backend_store):
        lines.append(f"(allow file-write* (subpath {_sandbox_quote(root)}))")
    # Standard device nodes, not data locations: a `(deny file-write*)` policy
    # otherwise blocks the /dev/null write that basic tooling (git included)
    # depends on. These are the null and standard-output sinks, so allowing them
    # widens no data-writable surface beyond the four sanctioned roots above.
    lines.append(
        '(allow file-write* (literal "/dev/null") (literal "/dev/stdout") (literal "/dev/stderr"))'
    )
    lines += [
        "",
        ";; Reads: deny-list of famous credential paths (D4). Not exhaustive.",
    ]
    for relative in SANDBOX_DENY_READ_DIRS + SANDBOX_DENY_READ_BROWSER_DIRS:
        lines.append(f"(deny file-read* (subpath {_sandbox_quote(home / relative)}))")
    for relative in SANDBOX_DENY_READ_FILES:
        lines.append(f"(deny file-read* (literal {_sandbox_quote(home / relative)}))")
    # Deny the whole keychain store, then allow the login keychain back (D4,
    # owner-amended). The allow must follow the deny so it wins.
    lines.append(f'(deny file-read* (subpath {_sandbox_quote(home / "Library/Keychains")}))')
    lines.append(
        f"(allow file-read* (literal {_sandbox_quote(home / SANDBOX_LOGIN_KEYCHAIN)}))"
    )
    lines.append(out_of_scope_deny)
    return "\n".join(lines) + "\n"


def sandbox_wrap(profile: Path | None) -> list[str]:
    # The launch-chain fragment that confines a backend: `sandbox-exec -f
    # <profile>`, inserted between caffeinate and the backend at the single argv
    # point so the whole backend process (file tools and MCP included) is
    # covered, not just its Bash calls (register D6). No profile means no wrap;
    # the fail-closed decision and the --unsafe-no-sandbox opt-out live in the
    # caller (see #21/#23).
    if profile is None:
        return []
    return [sandbox_exec_executable(), "-f", str(profile)]


def session_argv(backend_args: list[str], sandbox_profile: Path | None = None) -> list[str]:
    # The one place the wrapped Launch chain is assembled: the caffeinate power
    # assertion outermost, the `sandbox-exec` host-isolation wrap next (empty when
    # no profile is generated), the backend command innermost. Automated
    # iterations and `ralph resume` alike route through here so #19's sandbox wrap
    # lands as a single edit and the backend can never be launched unwrapped by an
    # adapter assembling the chain on its own.
    return [caffeinate_executable(), "-im", *sandbox_wrap(sandbox_profile), *backend_args]


def write_sandbox_profile(
    run_dir: Path, backend: str, worktree: Path, ralph_dir: Path, env: dict[str, str]
) -> Path:
    # Generate the concrete profile once per run (it is stable across a run's
    # iterations) and write it under the untracked .git/ralph run directory
    # (register D10): tracked source holds only the universal generator, never a
    # filled-in profile carrying the operator's home path. `ralph clean` removes
    # the whole .git/ralph tree, so this file with it.
    session_tmp = Path(env.get("TMPDIR") or "/tmp").resolve()
    profile_text = build_sandbox_profile(backend, worktree, ralph_dir, session_tmp, Path.home())
    path = run_dir / "sandbox.sb"
    path.write_text(profile_text, encoding="utf-8")
    return path


def sandbox_profile_for(
    backend: str, run_dir: Path, worktree: Path, ralph_dir: Path, env: dict[str, str]
) -> Path | None:
    # The Launch chain decides which backends run confined, once per run. Only
    # OpenCode is wrapped today (#20); the Claude wrap lands in #22 as the single
    # edit that lifts this gate. Returning None means no wrap and no written
    # profile, exactly as the loop's former `backend == "opencode"` branch did, so
    # a Claude run still generates no `sandbox.sb`.
    if backend != "opencode":
        return None
    return write_sandbox_profile(run_dir, backend, worktree, ralph_dir, env)


def shell_command(args: list[str], worktree: Path) -> str:
    return f"cd {shlex.quote(str(worktree))} && {shlex.join(args)}"


def resume_command(
    backend: str, model: str, worktree: Path, session_id: str, allow_agents: bool = False
) -> str:
    # A dedicated `ralph resume` re-establishes the full subscription trust
    # boundary (sanitized environment, per-session OAuth/routing proof, isolated
    # configuration, full-auto permissions, and caffeinate) before handing an
    # operator the interactive backend. A raw backend command would inherit the
    # operator's ambient environment and skip that proof, so recovery is routed
    # through Ralph itself. --session is placed last so callers can rely on it.
    args = [
        "ralph",
        "resume",
        "--backend",
        backend,
        "--model",
        model,
        "--worktree",
        str(worktree),
    ]
    # Reproduce the relaxed check so the handoff can re-prove the same boundary;
    # without it resume would refuse the very agents the run was allowed.
    if allow_agents:
        args.append("--unsafe-allow-agents")
    args += ["--session", session_id]
    return shell_command(args, worktree)


def restart_command(
    backend: str,
    model: str,
    worktree: Path,
    prompt_path: Path,
    remaining: int,
    timeout: float,
    allow_agents: bool = False,
) -> str:
    args = [
        "ralph",
        "run",
        str(prompt_path),
        "--backend",
        backend,
        "--iterations",
        str(remaining),
        "--model",
        model,
        "--worktree",
        str(worktree),
        "--timeout",
        str(timeout),
    ]
    if allow_agents:
        args.append("--unsafe-allow-agents")
    return shell_command(args, worktree)


class CaffeinateAssertion:
    def __init__(self, worktree: Path) -> None:
        self.worktree = worktree
        self.process: subprocess.Popen[str] | None = None

    def __enter__(self) -> CaffeinateAssertion:
        try:
            self.process = subprocess.Popen(
                [caffeinate_executable(), "-im", "-w", str(os.getpid())],
                cwd=self.worktree,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except OSError as error:
            raise RalphError(f"could not start caffeinate: {error.strerror}") from None
        try:
            returncode = self.process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            return self
        raise RalphError(f"caffeinate exited during startup with status {returncode}")

    def ensure_alive(self) -> None:
        # The loop-wide assertion must cover the entire invocation. If it exits
        # unexpectedly (killed, crashed) the sleep guarantee is gone, so the loop
        # stops safely at the next boundary rather than continuing unprotected.
        if self.process is None:
            return
        code = self.process.poll()
        if code is not None:
            raise RalphError(
                f"the loop-wide caffeinate power assertion exited unexpectedly with status {code}"
            )

    def __exit__(self, *_: object) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
