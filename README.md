# simple-ralph-loop

`ralph` repeatedly runs a UTF-8 prompt in fresh full-auto coding-agent
sessions, while refusing metered LLM API authentication. It is a macOS-only
personal helper for completing ordered GitHub issue work with a finite budget.

Before spending budget, Ralph proves three guarantees: **subscription-only
authentication** (no metered API billing), **customization isolation** (no
unproven backend agents, hooks, or plugins), and **host isolation** (the
backend session is confined by a Seatbelt sandbox so it cannot write outside
its sanctioned areas or read the operator's famous credential paths). Host
isolation defends against accident, not malice — see [Safety](#safety).

## Prerequisites

- macOS with `/usr/bin/caffeinate` and Python 3.11 or newer
- Git and an authenticated `gh` CLI, with a GitHub `origin` and named branch
- OpenCode 1.17.20 or newer, authenticated to OpenAI with ChatGPT OAuth only
- Claude Code 2.1.208 or newer, authenticated to a Claude Pro or Max
  subscription through `claude.ai` or `CLAUDE_CODE_OAUTH_TOKEN`

Run `opencode auth login` and choose OpenAI OAuth for ChatGPT. Run `claude` and
complete its interactive `claude.ai` login, or run `claude setup-token` and set
the returned subscription token as `CLAUDE_CODE_OAUTH_TOKEN`.

Ralph refuses API keys, custom endpoints, alternate providers, ambiguous
routing, and unsafe backend customizations. It removes known inference API
environment variables from child sessions without printing their values. Two
narrowly-scoped opt-outs, both described under [Run](#run), each relax exactly
one guarantee and nothing else: `--unsafe-allow-agents` opts a repository's
backend agents back in, and `--unsafe-no-sandbox` disables host isolation.

## Install

`simple-ralph-loop` is not published to PyPI, so install it from source with
pipx. From a checkout of this repository:

```sh
pipx install .
```

Or install directly from GitHub without cloning:

```sh
pipx install git+https://github.com/bmal/simple-ralph-loop.git
```

Either form registers the distribution as `simple-ralph-loop`, so upgrade and
uninstall by that name:

```sh
pipx upgrade simple-ralph-loop
pipx uninstall simple-ralph-loop
```

`pipx upgrade` reinstalls from the same source it was installed from (the local
checkout or the Git URL). Upgrading a checkout install re-reads that directory,
so `git pull` first.

## Run

```sh
ralph run prompt.md --backend opencode --iterations 5
ralph run prompt.md --backend claude --iterations 5
ralph run prompt.md --backend opencode --iterations 2 --timeout 5400
```

OpenCode defaults to `openai/gpt-5.6-sol`; Claude defaults to
`claude-opus-4-8`. Each run announces the resolved routing up front (for
example `ralph: backend claude, model claude-opus-4-8`) so the console states
exactly what the loop is about to spend budget on. Use `--model` for another
model in the selected subscription-backed provider and `--worktree PATH` to
target another GitHub worktree. Each iteration defaults to 3,600 seconds (60 minutes). A positive
`--timeout` changes the limit up to a maximum of 2,000,000 seconds; `--timeout 0`
deliberately disables it. Ralph raises the backend request and Bash-tool
timeouts to their maximum so they always outlast an accepted Ralph timeout and
never expire underneath legitimate work. Those backend limits are bounded
integers and cannot be made truly infinite, which is why a positive Ralph
timeout is capped below their ceiling; with `--timeout 0` they stay pinned at
maximum and Ralph's timer no longer applies.

By default both backends refuse a repository that carries their agents, because
an unattended billed run cannot prove which agents loaded. On Claude the agent
vectors are the `.claude/agents` directory and the `.claude/settings.json`
`agent` key; when such a vector is the *only* reason a repository is refused,
the error names `--unsafe-allow-agents` so the supported opt-out is
discoverable from the failure; every other refusal — a hooks or plugins
directory, managed or server-managed configuration, or any other unsafe settings
key, including when `agent` appears alongside one — keeps the plain message,
because the flag cannot relax those. On OpenCode, project and global agent
definitions load even under `--pure` and all surface in the effective
configuration's `agent` map, so a non-empty map is refused; that check runs
after every other preflight proof, so its refusal always names the opt-out, and
an effective configuration without an agent map is unfamiliar and fails closed.

Pass `--unsafe-allow-agents` when a repo's loop legitimately develops or
depends on agents: it admits the backend's agent vectors described above, and
warns that agent isolation is not proven for that run. The flag is deliberately
unsafe and narrowly scoped — it relaxes only those agent vectors. Hooks,
plugins, managed configuration, MCP routing, and every other unsafe setting
stay refused, and the runtime MCP/plugin/tool isolation proven from the
session's init event is unchanged. The same flag is accepted by `ralph resume`
with either backend, and Ralph reproduces it in the `resume` and `run` commands
it prints for a handed-off session so recovery re-establishes the same relaxed
boundary.

Every automated iteration and every recovery session is wrapped in a Seatbelt
sandbox (host isolation, described under [Safety](#safety)). Pass
`--unsafe-no-sandbox` only for a project genuinely incompatible with Seatbelt:
it skips the sandbox wrap and its self-test so the backend runs unconfined, and
it prints a loud stderr warning at launch. This flag is separate from and
orthogonal to `--unsafe-allow-agents` — it relaxes only host isolation, and
every other protection (subscription-only auth, customization isolation, secret
redaction) is untouched. `ralph resume` accepts it too, and Ralph reproduces it
verbatim in the `resume` and `run` commands it prints for a handed-off session
so a recovered session re-establishes the identical relaxed boundary.

Ralph snapshots the prompt once, starts a fresh session per iteration, and
stops early only when the final assistant output contains the exact standalone
line `<promise>COMPLETE</promise>`. Exhausting the budget without that marker is
an incomplete, non-zero result.

Questions, timeout, interruption, backend failure, or malformed output stop the
loop without an automatic retry. Ralph prints a `ralph resume` command for the
affected backend session and, when budget remains, a complete command for
starting a new Ralph invocation. `ralph resume` re-establishes the same
subscription-only trust boundary as an automated iteration: it sanitizes the
environment, re-proves authentication, effective routing, model availability,
and customization isolation, then relaunches the interactive session under
`caffeinate -im` with isolated configuration and full-auto permissions. It
therefore refuses a recovery environment that has gained an API credential,
custom endpoint, or unsafe backend customization since the handoff. A started
handed-off session consumes its iteration. The first Ctrl-C requests graceful
resumable shutdown; a second Ctrl-C force-kills the backend.

Known subscription credentials, including `CLAUDE_CODE_OAUTH_TOKEN`, are
redacted from readable progress and every retained diagnostic stream in case
backend output echoes an environment value.

Runtime prompts, options, structured output, diagnostics, session checkpoints,
and outcomes are retained under the selected worktree's resolved Git directory
at `.git/ralph/` (or the linked worktree's private Git directory). Remove only
that repository-local state when no loop is active with:

```sh
ralph clean --worktree PATH
```

## Safety

Ralph always grants dangerous full-auto permissions. The backend can edit
files and run commands without confirmation. Review the prompt, repository,
and effective authentication before starting an unattended run.

### Host isolation

Ralph confines every backend session in a Seatbelt sandbox it generates at
runtime and wraps the backend in via `/usr/bin/sandbox-exec` — the third proven
guarantee alongside subscription-only auth and customization isolation. The
policy is a **write allow-list** (the worktree, the resolved `.git/ralph` state,
the session tmp, and the running backend's own state directory) and a **read
deny-list** (`~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.config/gcloud`, `~/.azure`,
`~/.kube`, `~/.netrc`, `~/.docker/config.json`, `~/.npmrc`, `~/.pypirc`,
`~/.git-credentials` and `~/.config/git/credentials`, browser profiles,
`~/Library/Keychains`, and the *other* backend's auth store). Before
spending budget Ralph runs a one-shot self-test that must observe a denied read
and a denied write actually fail; if the sandbox cannot start or the self-test
fails open, Ralph fails closed and spends no budget. `ralph resume` and
handed-off recovery sessions are sandboxed identically because both route
through the same launch chain.

**This defends against accident, not malice.** It stops a well-meaning backend
from an errant `rm -rf` outside the worktree or an accidental `cat ~/.ssh/id_*`
swept into a commit or an LLM context. It does **not** stop a determined
exfiltrator: network egress stays fully open (the LLM API, `gh`/`git push`, and
package registries need it), so a backend that *decides* to leak a secret over
the network can. The read deny-list is the **famous credential paths, not a
completeness guarantee** — a credential in an unanticipated path stays readable,
and the self-test proves the listed denials bite, not that the list is
exhaustive. Do not mistake this for protection you did not build.

A few paths stay **readable on purpose** because the loop needs them:
`~/.config/gh` (so `gh` and `git push` keep working) and the running backend's
own auth store (its subscription token). One keychain file is a carve-out from
the `~/Library/Keychains` denial: `login.keychain-db` stays readable because on
a default macOS `gh` install the in-scope GitHub token lives there and cannot be
separated from it at the filesystem layer; every other keychain stays denied,
and the file is encrypted at rest so an accidental read yields ciphertext.
These are in-scope credentials the loop cannot function without, so this boundary
inherently cannot protect them; the *other* backend's auth store is denied
because the running session never needs it.

`caffeinate` remains the **outer** wrap of the launch chain
(`caffeinate -im sandbox-exec -f <profile> <backend> …`) so its power assertion
keeps working; the sandbox sits inside it and the backend innermost. The
concrete filled-in profile is written only under the untracked `.git/ralph/`;
tracked source holds only a universal template, so no home path, username, or
secret is ever committed to this repository.

`--unsafe-no-sandbox` (see [Run](#run)) loudly disables host isolation for a
project incompatible with Seatbelt. It is separate from `--unsafe-allow-agents`
and relaxes only host isolation. [ADR-0001](docs/adr/0001-host-isolation-via-seatbelt.md)
records why a Seatbelt sandbox was chosen over a container or VM (the industry
default for unattended full-auto) and why malice is deliberately out of scope.

Ralph holds `/usr/bin/caffeinate -im` assertions for automated and generated
manual sessions, preventing idle system and disk sleep while allowing display
sleep. It invokes the assertion tool by absolute path so a shadowed
`caffeinate` on `PATH` cannot replace it, and it stops the loop safely if the
loop-wide assertion exits unexpectedly. This cannot prevent sleep caused by
closing the laptop lid or an explicit sleep command, and it cannot protect
against power loss or external network and service outages. Keep the lid open
and provide adequate power.

Ralph keeps all runtime state beneath the selected worktree's resolved private
Git directory. It refuses a symlinked or unexpected file type anywhere in that
`.git/ralph` path, verifies recorded lock ownership before recovering a stale
lock, and `ralph clean` removes only that real state directory without
following symlinks or touching backend transcripts or source files.
