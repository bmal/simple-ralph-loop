---
Status: accepted
---

# Host isolation via ralph-orchestrated Seatbelt, scoped to accident not malice

## Context

Ralph runs backends in dangerous full-auto (`--dangerously-skip-permissions`
and equivalent) with no host confinement: the backend can edit any file and run
any command as the operator. Ralph's existing trust boundary proves *billing*
and *configuration provenance* (subscription-only auth, no unproven
agents/hooks/plugins) but deliberately does not protect the host machine. The
operator wants that gap closed with **no manual setup** and **no secrets in this
public repository**.

## Decision

Ralph generates a Seatbelt profile at runtime and wraps every backend session
in it via `/usr/bin/sandbox-exec` (absolute path, like `caffeinate`), keeping
ralph macOS-native and requiring zero operator setup. The policy is a **write
allow-list** (worktree, resolved `.git/ralph`, session tmp, the running
backend's own state dir) and a **read deny-list** (the famous credential paths —
`~/.ssh`, `~/.aws`, `~/.gnupg`, cloud/kube configs, browser profiles, OS
keychains — plus the *other* backend's auth store). `~/.config/gh` is
deliberately readable because the loop needs `gh`; such credentials the loop
requires are in-scope and cannot be protected. One keychain file is carved out
of the keychain denial: `~/Library/Keychains/login.keychain-db` stays readable
because on a default macOS `gh` install the in-scope GitHub token lives there and
cannot be separated from it at the filesystem layer (owner amendment to D4,
2026-07-17); every other keychain stays denied, and the file is encrypted at
rest so an accidental read yields ciphertext — consistent with the accident,
not malice scope. Network egress stays fully open.

**Owner amendment, 2026-09-01 (the in-scope backend set).** The read deny-list
named "the *other* backend's auth store", which assumed a run uses exactly one
backend. A run whose work dispatches to both model families — a two-family
adversarial review panel, for instance — needs both credentials, and for such a
run both are in-scope by D4's own criterion: a credential the loop requires
cannot be protected from it. Ralph therefore denies the auth store of every
backend the run has not **declared** in-scope, rather than every backend it is
not running. The set is declared per run via `--in-scope-backend` (repeatable, on
`run` and `resume`, reproduced into printed recovery commands), defaults to the
running backend alone, and is stated through the Run console as a relaxed
guarantee. A declaration lifts the denial on the single path the deny-list
already names and grants write on that one file so a token refresh persists; the
deny-list is never widened, shortened, or otherwise altered. Ralph additionally
pins `$XDG_DATA_HOME` for a declared OpenCode into the run directory, seeding
`auth.json` as a symlink to the operator's real credential — measured: OpenCode
rewrites that file in place, through the symlink, so a refresh reaches the real
credential rather than stranding a rotated token in the run directory.

**Accepted consequence:** `sandbox-exec` confines the whole process tree and
cannot nest (a second `sandbox-exec` inside a Ralph profile fails with
`sandbox_apply: Operation not permitted`, even under a bare `(allow default)`),
so a declared credential cannot be granted to one process alone — every command
in the run can read it. This is the identical exposure D4 already accepts for the
running backend's own token, with deliberate exfiltration out of scope per D1.
The grant is therefore gated on the operator's declaration rather than on actual
use, which Ralph cannot verify.

Both backends are wrapped uniformly — ralph proves the boundary rather than
trusting a backend to sandbox itself. Before spending budget ralph runs a
one-shot self-test proving a denied read and a denied write actually fail. If
the sandbox cannot start, ralph fails closed; `--unsafe-no-sandbox` (a separate,
narrowly-scoped opt-out mirroring `--unsafe-allow-agents`, reproduced into
printed `resume`/`run` commands) exists for projects incompatible with Seatbelt.
`ralph resume` is sandboxed identically. The concrete profile is written only
under the untracked `.git/ralph/`; tracked source contains only the universal
template, so no operator-specific path or secret is ever committed.

## Considered options

- **Container/VM (devcontainer, Docker Sandboxes, Lima, remote Firecracker).**
  The industry consensus for unattended full-auto, and categorically stronger.
  Rejected: it demands Docker plus manual credential plumbing (`claude
  setup-token`, repo-scoped `GH_TOKEN`, mounted `auth.json`), forces ralph off
  its macOS-only identity into a Linux runtime, and its extra strength only buys
  protection against *malice* — which is out of scope here.
- **Defer to Claude Code's native Bash sandbox.** Rejected: it covers only Bash
  (file tools, MCP, and hooks escape it — the docs call it "not sufficient for
  fully unattended runs"), it is Claude-only (OpenCode has no sandbox), and it
  would make ralph trust a backend's self-report instead of proving the
  boundary.

## Consequences

- **This defends against accident, not malice, and the README must say so as
  bluntly as it currently says "dangerous full-auto."** A read deny-list is
  leaky by nature: a credential in an unanticipated path stays readable, and an
  agent that *decides* to exfiltrate can. The self-test proves the listed
  denials bite; it cannot prove the list is exhaustive.
- **`gh`/`git push` under Seatbelt is the make-or-break compatibility risk** —
  Go-CLI TLS has a spotty record under Seatbelt. This must be prototyped before
  committing; Codex CLI proves it is achievable with the right profile.
- **`caffeinate` must remain the outer wrap** (`caffeinate -im sandbox-exec -f
  <profile> <backend>`); its power assertion is a host operation Seatbelt would
  otherwise block.
- Apple has deprecated `sandbox-exec`; the mechanism is load-bearing across the
  industry (Codex, Claude Code) but carries long-term uncertainty.
- **A declared in-scope credential is exposed to the whole run, not to one
  process** — see the 2026-09-01 amendment. The declaration is honour-system:
  Ralph cannot check that the run used the backend it named, only that the
  operator asked for it, and it states the relaxed guarantee on every run.
- **A token refresh is verified to write through the seeded symlink for
  OpenCode's in-place credential writes, not for a hypothetical
  write-temp-then-rename path.** Were OpenCode to change to an atomic rename, the
  symlink would be replaced by a regular file inside the run directory and that
  refresh would not reach the operator's real credential; the operator would
  re-authenticate, and the run directory would hold the rotated token until
  `ralph clean`. Not currently handled.
