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
`claude-opus-4-8`. Each run opens with a header stating the settings it
resolved and the directory its evidence will be retained in, before any budget
is spent:

```
ralph: backend claude, model claude-opus-4-8
ralph: iterations 4, timeout 3600s
ralph: repository example/project, branch main
ralph: worktree /Users/you/code/project
ralph: prompt /Users/you/code/project/prompt.md
ralph: interactive-only label may-ask-owner; children resolved before the first iteration
ralph: run directory /Users/you/code/project/.git/ralph/runs/20260810T101112.131415Z-0a1b2c3d
ralph: permissions dangerous full-auto; the backend may edit files and run commands without confirmation
ralph: power assertion caffeinate -im, which cannot prevent lid-close or explicit sleep, power loss, or external network and service outages
```

The concrete interactive-only children complete that header once Ralph has
resolved them, which it can only do after the first iteration's preflight has
proven `gh`. The last two lines are standing facts about every run, stated as
settings rather than shouted as warnings, so the rare real warning — a relaxed
guarantee, a dirty worktree — is worth reading when it appears.

Ralph writes its own lines to stderr, prefixed `ralph:` and coloured only when
stderr is a terminal and `NO_COLOR` is unset, so a redirected log carries no
escape sequences. On a terminal a header line that does not fit the window is
shortened — paths keep their informative end — rather than folded onto a second
row. Truncation is display-only: no retained artifact loses content.

Each iteration opens with a rule naming its number and the budget, and closes
with an outcome block giving its duration, outcome, session id, and the backend's
concluding message truncated for display. Once the first iteration's preflight
has proven authentication and customization isolation — the sandbox self-test
having already proven host isolation — the trust boundary is stated as proven
rather than left silent.

While an iteration runs, a single status line repaints in place on a terminal,
carrying the iteration and budget, the iteration's elapsed time, the current or
last tool, a running tool count, the backend orchestrator's live context size in
absolute tokens, and the number of live subagents — so a four-hour run and a
four-minute run occupy the same one line instead of filling your scrollback. Its
ticking clock is the only motion: there is no spinner, because a spinner that
keeps spinning over a hang answers the wrong question, while a clock that stops
tells you the run has stalled. The line is read against the terminal's live width
and never wraps — it drops fields from the right as the window narrows. When
stderr is not a terminal the line degrades to slow append-only heartbeats carrying
the same facts and no escape sequences, so a piped log stays clean. The backend's
own running commentary — its message text and bare tool markers — still prints on
stdout, separate from this stderr dashboard; a later change moves it behind an
opt-in so the default view is the dashboard alone.

Every run ends with a summary, on every terminal path
including a successful one that used to exit in silence: it names the final
branch, whether the worktree is clean, whether the branch's commits reached their
upstream, and the run directory where the evidence lives. A terminal bell rings
on every terminal outcome — completion, budget exhaustion, handoff, or error — so
an unattended run calls you back; a piped log carries no bell.

Once a run directory exists, every way a run can stop gets the full help block, not
one sentence. A question or a consuming stop prints the `RALPH NEEDS OPERATOR`
banner with the reason, the run id, the session to resume, the remaining budget, and
the exact recovery commands. Budget exhaustion adds the command that continues the
work — the same invocation with the budget restored — so you do not reconstruct it.
A backend failure that leaves evidence behind names what failed and points you at the
run directory to inspect, rather than dropping a bare line. Only argument and
precondition failures, which happen before anything is on disk, stay one-liners. A
relaxed guarantee — `--unsafe-no-sandbox` or admitted backend agents — is stated
loudly as a deviation warning, distinct from the standing settings above it.

Use `--model` for another
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
stops early only when the final turn's last message from the backend itself
contains the exact standalone line `<promise>COMPLETE</promise>`. Exhausting the
budget without that marker is an incomplete, non-zero result.

Some child issues embed an owner decision and must be worked interactively. The
Loop protocol Ralph appends to every prompt names a configurable label — set with
`--interactive-label` on `ralph run`, defaulting to `may-ask-owner` — and tells
the backend to treat a child carrying it as blocked for this iteration: select the
next unblocked child that does not carry it, and when only labelled children
remain, halt with `<promise>NEEDS_INPUT</promise>` naming them rather than claiming
completion. So the backend is given facts rather than a rule to apply from memory,
Ralph resolves the concrete open issues carrying the label — once per run, through
the same `gh` dependency preflight already proves, before the first session spends
budget — and injects their numbers into the prompt. A failed or malformed query
fails the run closed, like every other preflight proof; an empty result set is
stated as empty rather than omitted. The honest limit: this selection is
*advisory*. Ralph cannot observe which child the backend actually picks, so it
cannot enforce the choice — the same class of prompt-level control that steering
the backend away from background subagents used to be. Injecting the concrete
numbers narrows the gap but does not close it. What *is* mechanical is the
escalation: the needs-input marker is detected and halts the run. `ralph resume`
takes no label, because recovery is already the interactive session the label
exists to demand.

A Claude session may run across several turns. Claude Code's Agent/Task subagent
tool defaults to background execution, and a subagent that finishes after the
turn that launched it makes the CLI open a second turn with a fresh init. Ralph
reads the session to end of stream and accepts these multi-turn streams instead
of ending the run: every turn's init re-proves the full trust boundary
(subscription-only auth, full-auto mode, no external MCP servers, plugins, or
unknown tools, and the same session id), the turns' results are attributed in
order, and the iteration is judged on the final turn's last message from the
backend itself — a completion claim superseded by anything the backend said
afterwards, in that turn or an earlier one, does not stop the run early, and a
needs-input marker it superseded the same way warns on stderr and continues
instead of halting. A second init is admitted only in a session that registered
a background task, carrying the session's own id, while a turn was open; an
unexplained duplicate init, one explained only by a registration that preceded
every turn, and one explained only by a registration bearing a foreign id, still
fail closed.
What may follow the results is judged by *shape*, not by a list of tolerated
subtypes: whenever a session has touched a background task, Claude Code emits
teardown and telemetry after the result block (a task summary, a killed-task
update, a notification, a drained registration, and — after a parked task — a
re-init), and Ralph ignores all of it — recognised or not — so a future release
that adds another such event is not an outage. A post-result init on its own is
teardown: it opens no turn, and failing on it once closed every background-using
iteration. It is still validated exactly as any init — the same session id and
full trust boundary are re-proved at this position, so a crossed session id or
unsafe metadata fails closed rather than slipping in as teardown — but once valid
it opens no turn. It fails closed only when a turn actually *follows* it — an
assistant message or a further result, closing a continuation Ralph cannot place
— and that follow-up is named as the per-turn result flush a build closing each
turn with its own result would produce. A bare post-result assistant, or a result
beyond the turn count, is an ordinary contract violation. Which messages are the
Backend's own is read from an origin marker (`parent_tool_use_id`) present on every
assistant event — null on the Backend's own, a tool-use id on a subagent's — so a
subagent's own messages stay in the retained stream evidence but never count as the
Backend's answer or as a completion/needs-input marker. Three malformed shapes the
real CLI never emits are refused closed rather than credited: an assistant event
that omits the marker entirely (it would default to the Backend and let a subagent
assemble the answer), a subagent event carrying a foreign session id (a crossed
stream), and the background registration above bearing a foreign id. The honest
limit: the only bound on a background subagent that never drains is `--timeout`
— Ralph adds no separate idle detection, so a wedged background task can hold an
iteration open until the timeout fires. Resuming a handed-off session that still held a background task
replays that task's notification as a leading turn, harmless in the interactive
`ralph resume` (which is why headless resume is not attempted).

A background task the backend leaves running when it *ends its turn* is a
different hazard. What happens to that task once the backend stops has two observed
outcomes and neither is guaranteed: teardown may kill it, or it may finish and
reappear as a fresh continuation Ralph cannot attribute to the ended turn (the
post-result teardown init above, validated but never opened as a turn). Either
way the backend has silently abandoned its own work, typically at "pushed to main,
now verify CI", counting on a later notification that may never come. Ralph works
this from both ends. The Claude prompt carries an adapter-local directive (OpenCode,
with no background-task runtime, does not): the backend may launch background work
but must not end its turn while it is still unresolved, must not rely on a later
delivery to resume it, and must instead bring the work into the turn or cancel it
and say what it left unverified. And when the CLI's teardown explicitly
reports it killed a task anyway, Ralph names the abandoned task on the same stderr
stream it uses for its other mid-run warnings, so the operator learns which work
went unverified. That report is observational — detected only from the CLI's
explicit killed-task update, never inferred from a task-list drain (which also
fires on an ordinary mid-turn completion) — and it leaves the iteration's outcome
and final message exactly as they were. The killed update must carry the session's
own id to be attributed: a foreign, missing, or empty session id is a crossed
stream and names no task, and a duplicate report of a task already recorded adds
no second one. Once a valid killed update is accepted, its warning is delivered on
every terminal path — an ordinary completion and a contract-failure handoff alike
— so a later stream event that fails the contract cannot suppress the work the CLI
already reported abandoned.

Questions, timeout, interruption, backend failure, or malformed output stop the
loop and hand off for manual recovery; Ralph spends exactly one session per
iteration and never restarts one itself.
Ralph prints a `ralph resume` command for the
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
files and run commands without confirmation. Every run's header states this,
along with what the `caffeinate` power assertion cannot protect against. Review
the prompt, repository, and effective authentication before starting an
unattended run.

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
