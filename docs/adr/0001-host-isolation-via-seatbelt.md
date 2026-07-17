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
