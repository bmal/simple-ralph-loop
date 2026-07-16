# simple-ralph-loop

`ralph` repeatedly runs a UTF-8 prompt in fresh full-auto coding-agent
sessions, while refusing metered LLM API authentication. It is a macOS-only
personal helper for completing ordered GitHub issue work with a finite budget.

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
environment variables from child sessions without printing their values. The
one scoped exception is `--unsafe-allow-agents`, described under
[Run](#run), which opts a repository's backend agents back in.

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
ralph run prompt.md --backend opencode --iterations 2 --timeout 3600
```

OpenCode defaults to `openai/gpt-5.6-sol`; Claude defaults to
`claude-opus-4-8`. Each run announces the resolved routing up front (for
example `ralph: backend claude, model claude-opus-4-8`) so the console states
exactly what the loop is about to spend budget on. Use `--model` for another
model in the selected subscription-backed provider and `--worktree PATH` to
target another GitHub worktree. Each iteration defaults to 2,700 seconds (45 minutes). A positive
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
