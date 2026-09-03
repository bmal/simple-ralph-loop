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
profile is generated — both backends confined uniformly (#20 OpenCode, #22 Claude)
through one backend-aware generator. ``run_sandbox_self_test`` is the one-shot proof (#21)
that the generated profile actually bites — a denied read and a denied write must
both fail — that the Loop runs once per run before spending budget.

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
  the deny-list is backend-aware so the auth store of every backend the run has
  not declared in-scope is denied, while the stores it needs stay readable (D4).
  The login keychain is allowed back after the keychain deny so the allow wins
  (owner-amended D4).
- ``in_scope`` is the run's declared set of additional backends (D3/D4 owner
  amendment, 2026-09-01). D4's original rule denied "the other backend", which
  assumed one backend per run; a run whose work dispatches to both families needs
  both credentials, so the deny is conditioned on what the run declared rather
  than on what it launches. Declaring nothing yields a byte-identical profile to
  before the amendment, and a declaration grants exactly the one credential path
  D4 already names -- never a widened deny-list and never another entry removed.
  ``prepare_in_scope_state`` pins ``$XDG_DATA_HOME`` into the run directory for a
  declared OpenCode and seeds its ``auth.json`` as a symlink to the operator's
  real credential, so a mid-run token refresh writes through and the store does
  not accumulate in the operator's home.
- The concrete filled-in profile is written only under the untracked ``.git/ralph``
  run directory (D10); tracked source holds only the universal generator.
- ``run_sandbox_self_test`` fails closed before any budget is spent (register D8):
  a denied read and a denied write must both be observed to fail under the
  generated profile, or the run stops — a profile that permits either (parsed but
  failed open) or a probe that cannot run at all is refused. The probe runner is
  injectable, and ``default_sandbox_probe``'s ``home`` argument is an internal
  test seam (the qualification smoke points the probes at a hermetic home); like
  the ``RALPH_*`` overrides, production supplies neither.
- ``establish_sandbox`` is silent: under ``--unsafe-no-sandbox`` it returns no
  profile and words nothing. Stating the relaxed guarantee loudly is the Run
  console's job now (register G7/G13); the gate emits only the fact (no profile),
  and its callers — the Loop and ``cli`` resume — raise the deviation through the
  console.
- Recovery commands route through ``ralph resume`` / ``ralph run`` so replayed
  recovery re-establishes the full Trust boundary rather than inheriting the
  operator's ambient environment; ``--unsafe-allow-agents`` is reproduced so resume
  re-proves the same relaxed boundary, and ``--session`` is placed last.
- The loop-wide ``CaffeinateAssertion`` must cover the entire invocation: if it
  exits unexpectedly the sleep guarantee is gone, so ``ensure_alive`` fails closed
  and the loop stops at the next boundary rather than continuing unprotected. Which
  of its two failures an operator is told about follows from the iteration the run
  had reached -- named by the caller -- never from how promptly the failure
  surfaced: a loss found before the first session is the startup failure the probe
  names, however long the assertion took to die, and only a loss found after one
  has begun is the mid-run one.

Depends on / must not know: ``errors``. It must not know how the Loop schedules
Iterations or how a Backend consumes the argv it helps build.

See also: ``process`` (per-Iteration process control), ``loop`` (holds the
CaffeinateAssertion and hands the run's facts to the Run console), the Backend
adapters (wrap their argv).
"""

from __future__ import annotations

from collections.abc import Sequence
import os
from pathlib import Path
import shlex
import subprocess
from typing import Callable

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
# in-scope siblings such as ~/.config/gh, ~/.config/git/config, or ~/.docker/
# contexts). `.git-credentials` and its XDG twin are git's `store` helper's
# plaintext token files: famous, and squarely in the loop's own git/gh domain,
# so an accidental read of one is the exact accident register D1 scopes in.
SANDBOX_DENY_READ_FILES = (
    ".netrc",
    ".docker/config.json",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    ".config/git/credentials",
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

# The self-test's write probe target: a file directly under the operator's home
# root, which is never one of the sanctioned write roots, so a profile that bites
# refuses the create (register D8). Named distinctively so an accidental leftover
# left by a fail-open profile is unmistakable.
SANDBOX_WRITE_PROBE = ".ralph-sandbox-selftest-write-probe"

# World-writable scratch roots the backend may write to unconditionally (register
# D3, amended). By convention these hold no operator work product or secrets —
# they are shared, sticky-bit temp — and the confined backend runs as the
# operator's own uid, so it can already write here anyway. Allow-listing them is
# what keeps a wrapped session usable: Claude Code creates its per-session working
# directory under `/private/tmp/claude-<uid>/…` (NOT under $TMPDIR), so without
# these roots every Bash call the backend makes fails EPERM on its first mkdir.
# `/tmp` is a symlink to `/private/tmp`; both are listed so the rule matches
# whichever spelling a tool uses. $TMPDIR (the darwin per-user temp) is a
# separate valueless scratch root and is allowed via `session_tmp`.
SANDBOX_WORLD_WRITABLE_TMP_ROOTS = (Path("/private/tmp"), Path("/tmp"))

# The single credential each Backend authenticates from, home-relative, with the
# Seatbelt filter its grant needs. OpenCode keeps its subscription OAuth in one
# file, so a grant for it is one literal path; Claude's store is a directory
# tree, so its grant is that subpath -- the same shape it already gets while it
# is the running backend. A backend the run has not declared in-scope has this
# exact path denied (register D4); a declared one has it granted, and nothing
# wider (D3/D4 owner amendment, 2026-09-01).
SANDBOX_BACKEND_CREDENTIAL = {
    "claude": ("subpath", ".claude"),
    "opencode": ("literal", ".local/share/opencode/auth.json"),
}
# Where Ralph pins $XDG_DATA_HOME for a run that declares OpenCode in-scope
# without running it: a per-run directory under the run dir, seeded with an
# auth.json symlink to the operator's real credential. This keeps the 2.6 GB
# session database, logs and snapshots out of the operator's home, makes the
# credential grant one literal file instead of a whole store, and leaves
# evidence in the run dir that OpenCode actually ran.
SANDBOX_XDG_DIRNAME = "xdg"

# Self-test probe outcomes: the sandbox refused the operation (the profile bit),
# permitted it (the profile parsed but failed open), or the probe could not be
# run at all. Anything but BLOCKED stops the run fail-closed before budget.
PROBE_BLOCKED = "blocked"
PROBE_ALLOWED = "allowed"
PROBE_UNAVAILABLE = "unavailable"

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


def _backend_store(backend: str, home: Path, data_home: Path | None) -> Path:
    # A Backend's own state directory: the D3 write root it gets while it is the
    # running backend. OpenCode's follows $XDG_DATA_HOME, which it honors for its
    # whole store -- session database, logs, worktree snapshots and auth.json
    # alike -- so the root is derived from the session environment rather than
    # assumed at ~/.local/share. An operator who exports XDG_DATA_HOME would
    # otherwise get a write allow-list pointing at a directory OpenCode is not
    # using, and every run would die on its first startup write.
    if backend == "claude":
        return home / ".claude"
    return (data_home or home / ".local" / "share") / "opencode"


def build_sandbox_profile(
    backend: str,
    worktree: Path,
    ralph_dir: Path,
    session_tmp: Path,
    home: Path,
    in_scope: Sequence[str] = (),
    data_home: Path | None = None,
) -> str:
    # Produce the Seatbelt (`sandbox-exec`) profile text confining a backend
    # session (register D2/D3/D4/D5/D10). Pure: it maps already-resolved absolute
    # paths plus the backend name to policy text and consults nothing else, so no
    # secret can be interpolated and the same inputs always yield the same
    # profile. Reads stay permissive with a deny-list of famous credential paths;
    # writes are an allow-list of the sanctioned roots; network egress is
    # unrestricted. The deny-list is backend-aware: the auth store of every
    # backend the run has *not* declared in-scope is denied, while the stores the
    # run actually needs stay readable (D4).
    #
    # `in_scope` names the additional backends this run declares it will use. D4
    # originally denied "the other backend", which assumed a run uses exactly one
    # -- true for an automated Iteration, false for a run whose work dispatches to
    # both families (a two-model review panel). For such a run both credentials
    # are in-scope by D4's own criterion: a credential the loop requires cannot be
    # protected from it. Undeclared is the default, so a run that does not ask for
    # a second backend gets a byte-identical profile to before the amendment.
    declared = {backend, *in_scope}
    backend_store = _backend_store(backend, home, data_home)

    lines = [
        "(version 1)",
        ";; Ralph host isolation (register D2-D5/D10). Defends against accident,",
        ";; not malice: reads are a deny-list of famous credential paths (not a",
        ";; completeness guarantee) and network egress stays open by design.",
        "(allow default)",
        "",
        ";; Writes: allow-list of the sanctioned roots only (D3); deny all else.",
        ";; The four per-run roots plus the world-writable temp roots, which hold",
        ";; no operator work product and are where the backend's own harness (e.g.",
        ";; Claude Code under /private/tmp/claude-<uid>) creates its session dir.",
        "(deny file-write*)",
    ]
    for root in (worktree, ralph_dir, session_tmp, backend_store, *SANDBOX_WORLD_WRITABLE_TMP_ROOTS):
        lines.append(f"(allow file-write* (subpath {_sandbox_quote(root)}))")
    # A declared additional backend needs its credential writable, not just
    # readable: a subscription OAuth access token expires and the CLI rewrites
    # the file in place on refresh (measured: OpenCode preserves the inode and
    # writes straight through a symlink). Without the write the refresh EPERMs
    # mid-run. The grant is that one credential path and nothing around it -- the
    # rest of the declared backend's store stays denied.
    for name in sorted(declared - {backend}):
        kind, relative = SANDBOX_BACKEND_CREDENTIAL[name]
        lines.append(f"(allow file-write* ({kind} {_sandbox_quote(home / relative)}))")
    # Standard device nodes, not data locations: a `(deny file-write*)` policy
    # otherwise blocks the /dev/null write that basic tooling (git included)
    # depends on. These are the null and standard-output sinks, so allowing them
    # widens no data-writable surface beyond the sanctioned roots above.
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
    # Every backend this run did not declare in-scope: its credential is denied by
    # the exact path D4 names, never by a widened or removed entry (D4 amendment).
    for name in sorted(set(SANDBOX_BACKEND_CREDENTIAL) - declared):
        kind, relative = SANDBOX_BACKEND_CREDENTIAL[name]
        lines.append(f"(deny file-read* ({kind} {_sandbox_quote(home / relative)}))")
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
    run_dir: Path,
    backend: str,
    worktree: Path,
    ralph_dir: Path,
    env: dict[str, str],
    in_scope: Sequence[str] = (),
) -> Path:
    # Generate the concrete profile once per run (it is stable across a run's
    # iterations) and write it under the untracked .git/ralph run directory
    # (register D10): tracked source holds only the universal generator, never a
    # filled-in profile carrying the operator's home path. `ralph clean` removes
    # the whole .git/ralph tree, so this file with it.
    session_tmp = Path(env.get("TMPDIR") or "/tmp").resolve()
    # OpenCode's whole store follows $XDG_DATA_HOME, so the write allow-list must
    # be derived from the environment the session will actually run under -- the
    # value Ralph pinned for a declared in-scope OpenCode, or the operator's own
    # ambient setting -- rather than assumed at ~/.local/share.
    raw_data_home = env.get("XDG_DATA_HOME")
    data_home = Path(raw_data_home).resolve() if raw_data_home else None
    profile_text = build_sandbox_profile(
        backend, worktree, ralph_dir, session_tmp, Path.home(), in_scope, data_home
    )
    path = run_dir / "sandbox.sb"
    try:
        path.write_text(profile_text, encoding="utf-8")
    except OSError as error:
        # A profile that cannot be written means the boundary cannot be
        # established: fail closed before any budget is spent (register D7)
        # rather than launch the backend unconfined.
        raise RalphError(
            f"could not write the host-isolation sandbox profile: {error.strerror}"
        ) from None
    return path


def sandbox_profile_for(
    backend: str,
    run_dir: Path,
    worktree: Path,
    ralph_dir: Path,
    env: dict[str, str],
    in_scope: Sequence[str] = (),
) -> Path:
    # The Launch chain confines every backend under Ralph's own profile, once per
    # run — Ralph proves the boundary rather than trusting a backend to sandbox
    # itself (register D6). Both OpenCode (#20) and Claude (#22) route through the
    # same backend-aware generator, so there is no per-backend fork here beyond the
    # deny/allow inputs `build_sandbox_profile` already keys off the backend name.
    # The `--unsafe-no-sandbox` opt-out that can turn the wrap off is honored one
    # level up in `establish_sandbox`, which short-circuits before ever calling
    # this; a call here always means a confined session (register D7).
    return write_sandbox_profile(run_dir, backend, worktree, ralph_dir, env, in_scope)


def _existing_denied_read_dir(home: Path) -> Path:
    # The read probe must target a deny-listed directory that actually exists, or
    # a fail-open profile (which would let the read through) is indistinguishable
    # from a plain "no such file". Prefer whichever famous credential directory is
    # present on this machine, so the probe is non-vacuous in essentially every
    # real case; fall back to ~/Library/Keychains, which is denied wholesale
    # (login.keychain-db is allowed back by exact path, but listing the directory
    # still needs read on the directory itself) and is present on any macOS login
    # account. In the vanishing case where none exist the read degrades to a plain
    # failure, but the write probe still catches a fail-open profile.
    keychains = home / "Library" / "Keychains"
    for relative in SANDBOX_DENY_READ_DIRS + SANDBOX_DENY_READ_BROWSER_DIRS:
        candidate = home / relative
        if candidate.is_dir():
            return candidate
    return keychains


def _sandbox_probe_command(kind: str, home: Path) -> list[str]:
    # The concrete probe a self-test runs under the generated profile. Both are
    # non-vacuous: outside the sandbox each operation genuinely succeeds, so a
    # profile that failed open is distinguishable from one that bit.
    if kind == "read":
        # A denied read of an existing, deny-listed path (register D4).
        return ["/bin/ls", "--", str(_existing_denied_read_dir(home))]
    # A denied write outside the sanctioned roots (register D3): the home root is
    # never a write root, so a profile that bites refuses the create.
    return ["/bin/sh", "-c", f"printf x > {shlex.quote(str(home / SANDBOX_WRITE_PROBE))}"]


def default_sandbox_probe(profile: Path, kind: str, home: Path | None = None) -> str:
    # Run one self-test probe under the generated profile via `sandbox-exec -f
    # <profile>` (register D8), resolved by absolute path exactly like the launch
    # wrap so a PATH-shadowed launcher cannot answer for the boundary. Returns
    # whether the sandbox refused the operation (PROBE_BLOCKED), permitted it
    # (PROBE_ALLOWED — the profile failed open), or could not be run at all
    # (PROBE_UNAVAILABLE). The `home` seam lets the qualification smoke point the
    # probes at a hermetic synthetic home; production reads the operator's own.
    home = home or Path.home()
    command = _sandbox_probe_command(kind, home)
    try:
        result = subprocess.run(
            [sandbox_exec_executable(), "-f", str(profile), *command],
            capture_output=True,
            text=True,
            # A probe that cannot be launched, or hangs, must fail closed rather
            # than stall the pre-loop gate: the probe itself is trivial (list a
            # directory / attempt one write), so a generous ceiling only ever
            # trips on a wedged launcher.
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return PROBE_UNAVAILABLE
    permitted = result.returncode == 0
    if kind == "write" and permitted:
        # A fail-open profile actually created the probe file; remove it so a
        # broken sandbox does not also litter the operator's home.
        try:
            (home / SANDBOX_WRITE_PROBE).unlink()
        except OSError:
            pass
    return PROBE_ALLOWED if permitted else PROBE_BLOCKED


def run_sandbox_self_test(
    profile: Path, runner: Callable[[Path, str], str] | None = None
) -> None:
    # The one-shot proof that the generated profile actually bites before any
    # budget is spent (register D8): a denied read and a denied write must both be
    # observed to fail under the profile. If either is permitted (the profile
    # parsed but failed open) or a probe cannot be run at all, the run stops
    # fail-closed with a clear error, exactly as the caffeinate startup assertion
    # does — Ralph never spends budget on an unproven sandbox. The probe runner is
    # injectable so a test can feed a deterministic outcome without a real kernel;
    # production uses `default_sandbox_probe`, which probes via `sandbox-exec`.
    probe = runner or default_sandbox_probe
    for kind in ("read", "write"):
        outcome = probe(profile, kind)
        if outcome == PROBE_ALLOWED:
            raise RalphError(
                f"host isolation self-test failed open: the sandbox permitted a denied "
                f"{kind}, so Ralph cannot prove host isolation; refusing to spend budget"
            )
        if outcome != PROBE_BLOCKED:
            raise RalphError(
                f"host isolation self-test could not run its {kind} probe under "
                "sandbox-exec; refusing to spend budget on an unproven sandbox"
            )


def establish_sandbox(
    backend: str,
    run_dir: Path,
    worktree: Path,
    ralph_dir: Path,
    env: dict[str, str],
    *,
    no_sandbox: bool,
    in_scope: Sequence[str] = (),
) -> Path | None:
    # The single fail-closed gate that turns a session's host-isolation intent
    # into a proven boundary, shared by automated iterations (`run`) and
    # interactive recovery (`resume`) so both are confined identically (register
    # D9). Absent the opt-out it generates the per-run profile and proves it
    # actually bites via the one-shot self-test before any budget is spent
    # (register D7/D8): a profile that cannot be built or written, or one that
    # fails open, stops the session right here rather than launching unconfined.
    # `--unsafe-no-sandbox` relaxes only host isolation (register D7): no profile and
    # no self-test — every other guarantee (subscription-only auth, customization
    # isolation, redaction) is untouched because this gate governs nothing else. The
    # loud warning that the boundary is unproven is the Run console's to word now
    # (register G7/G13): the gate returns the fact (no profile) and its two callers —
    # the Loop and ``cli`` resume — state the deviation through the console.
    if no_sandbox:
        return None
    profile = sandbox_profile_for(backend, run_dir, worktree, ralph_dir, env, in_scope)
    run_sandbox_self_test(profile)
    return profile


def additional_in_scope(backend: str, in_scope: Sequence[str]) -> list[str]:
    # The backends a run declared in-scope *beyond* the one it runs. Declaring the
    # running backend is a no-op (its store is in-scope by D4 already), so it is
    # folded out here once and every caller -- the profile, the state seeding, the
    # console deviations, the recovery commands -- agrees on the same set.
    return sorted(set(in_scope) - {backend})


def prepare_in_scope_state(
    run_dir: Path, backend: str, in_scope: Sequence[str], home: Path | None = None
) -> dict[str, str]:
    # Environment additions that pin a declared in-scope backend's state into the
    # run directory, and the on-disk seeding that makes them work. Returned rather
    # than applied so the caller layers them onto the sanitized session
    # environment explicitly; an empty dict means the run declared nothing extra.
    #
    # Only OpenCode needs this. It honors $XDG_DATA_HOME for its entire store, so
    # pinning the variable at `<run_dir>/xdg` keeps its session database, logs and
    # worktree snapshots (2.6 GB on the author's machine) out of the operator's
    # home, leaves per-run evidence that OpenCode actually ran, and -- because the
    # only thing left in the real store is the credential -- lets the D3 grant be
    # that one file instead of the whole directory tree. auth.json is seeded as a
    # symlink to the operator's real credential so a token refresh during the run
    # writes through to it (measured: OpenCode rewrites the file in place,
    # preserving the inode) instead of stranding a rotated token in the run dir.
    #
    # The running backend's own store is deliberately left alone: pinning it would
    # relocate an operator's existing OpenCode session history and put it behind
    # `ralph clean`, a behavior change no reported problem asks for.
    home = home or Path.home()
    if "opencode" not in additional_in_scope(backend, in_scope):
        return {}
    xdg = run_dir / SANDBOX_XDG_DIRNAME
    store = xdg / "opencode"
    credential = home / SANDBOX_BACKEND_CREDENTIAL["opencode"][1]
    try:
        store.mkdir(parents=True, exist_ok=True)
        link = store / "auth.json"
        if not link.is_symlink() and not link.exists():
            link.symlink_to(credential)
    except OSError as error:
        # The declared backend cannot authenticate without this, and a session
        # that silently loses a declared lane is worse than one that stops:
        # fail closed here, before any budget is spent (register D7).
        raise RalphError(
            f"could not prepare the in-scope OpenCode state directory: {error.strerror}"
        ) from None
    return {"XDG_DATA_HOME": str(xdg)}


def shell_command(args: list[str], worktree: Path) -> str:
    return f"cd {shlex.quote(str(worktree))} && {shlex.join(args)}"


def resume_command(
    backend: str,
    model: str,
    worktree: Path,
    session_id: str,
    allow_agents: bool = False,
    no_sandbox: bool = False,
    in_scope: Sequence[str] = (),
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
    # Reproduce each relaxed check so the handoff re-establishes the identical
    # boundary; the two unsafe flags are orthogonal and reproduce independently
    # (register D7). Without --unsafe-allow-agents resume would refuse the very
    # agents the run allowed; without --unsafe-no-sandbox it would re-confine a
    # session the operator deliberately ran unconfined. --session stays last.
    if allow_agents:
        args.append("--unsafe-allow-agents")
    if no_sandbox:
        args.append("--unsafe-no-sandbox")
    # Every declared in-scope backend reproduces too: without them the resumed
    # session would re-confine a lane the run deliberately opened, and the
    # handoff would fail on the half of its work the operator came back to finish.
    for name in additional_in_scope(backend, in_scope):
        args += ["--in-scope-backend", name]
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
    no_sandbox: bool = False,
    in_scope: Sequence[str] = (),
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
    # Each unsafe flag reproduces independently so the continuation run re-proves
    # the same relaxed boundary and nothing more (register D7).
    if allow_agents:
        args.append("--unsafe-allow-agents")
    if no_sandbox:
        args.append("--unsafe-no-sandbox")
    for name in additional_in_scope(backend, in_scope):
        args += ["--in-scope-backend", name]
    return shell_command(args, worktree)


def _startup_failure(code: int) -> RalphError:
    # The one wording for a power assertion that never got going, shared by the
    # startup probe and by the first iteration's check so the two can never drift.
    return RalphError(f"caffeinate exited during startup with status {code}")


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
        raise _startup_failure(returncode)

    def ensure_alive(self, iteration: int) -> None:
        # The loop-wide assertion must cover the entire invocation. If it exits
        # unexpectedly (killed, crashed) the sleep guarantee is gone, so the loop
        # stops safely at the next boundary rather than continuing unprotected.
        if self.process is None:
            return
        code = self.process.poll()
        if code is None:
            return
        # A caffeinate that failed slower than the startup probe waits is the same
        # failure the probe names, not a mid-run loss: nothing has been spent yet.
        if iteration == 1:
            raise _startup_failure(code)
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
