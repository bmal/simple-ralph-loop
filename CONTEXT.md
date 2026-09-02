# simple-ralph-loop

A macOS-only personal loop runner that repeatedly executes one prompt in fresh
full-auto coding-agent sessions against a finite iteration budget.

## Language

**Backend**:
The coding-agent CLI (OpenCode or Claude Code) that ralph launches to execute
an iteration.
_Avoid_: agent (reserved for backend-defined subagents), model, provider

**Iteration**:
One fresh backend session running the snapshotted prompt; consumes one unit of
budget, including when handed off for manual recovery.
_Avoid_: run, loop cycle

**Launch chain**:
The ordered stack of wrappers every backend session starts under — the
`caffeinate` power assertion outermost, host isolation inside it, the backend
innermost — identical for automated iterations and handed-off recovery.
_Avoid_: launcher, wrapper, command line

**In-scope backend**:
A Backend whose subscription credential this run is allowed to read. Always the
Backend ralph launches; additionally, any the operator declares with
`--in-scope-backend` because the run's own work dispatches to it. Every Backend
*not* in the set has its credential denied by the host-isolation profile. The set
is per-run and declared, not observed — host isolation confines the whole process
tree at once, so a declared credential is readable by every command in the run.
_Avoid_: allowed backend, secondary backend, extra backend

**Loop protocol**:
The contract ralph appends to every prompt telling the backend how to signal
an iteration's outcome (complete, or needs operator input) and its progress (the
Stage it has reached) via markers ralph can detect. Built once per run from the
configured interactive-only label and the concrete children ralph resolves as
carrying it, so the same contract, its resolved facts, and its marker parser stay
in one module. Progress and outcome share the marker envelope but never each
other's meaning: a Stage declaration completes nothing and halts nothing, and is
excluded from the prose the needs-input heuristics read.
_Avoid_: prompt suffix, prompt template, system prompt

**Stage**:
Which part of the operator's own prompt the backend has reached — selecting a
task, loading context, implementing, finishing — declared by the backend through
the Loop protocol and shown on the status line as the answer to "what is it
doing", in place of the tool name rather than beside it. Never inferred from tool
use: the stages live in
the prompt, which ralph snapshots but never reads, so a guess would confidently
report the wrong one and be believed. The label is free text in the backend's own
wording, because no fixed vocabulary here could name another prompt's phases; the
protocol suggests wording rather than enumerating it, and the parser bounds and
sanitizes what comes back. A stage that has gone stale — declared long ago with no
transition announced since — is dropped in favour of the last tool rather than
going on asserting a phase that may have ended. Both adapters declare one, each
reading the marker out of its own stream shape.
_Avoid_: step and phase (both collide with OpenCode's own step-start/step-finish
stream events), status, state, progress bar

**Parked turn**:
A backend turn ended while a background task it launched is still unresolved, on the
false belief the session will be re-invoked when the task completes. It cannot bank
on that: an unresolved task has two observed outcomes and neither is guaranteed —
teardown may kill it, or it may finish and reappear as a fresh continuation Ralph
cannot attribute to the ended turn (the trailing teardown init validated but not
opened as a turn) — so either way the work is abandoned, not deliberately
resumed. The Claude adapter prevents it with an adapter-local prompt directive that
names both outcomes and forbids relying on a later delivery (never launching is fine
— only abandoning is forbidden) and, when the CLI reports it killed a task anyway,
names the abandoned task on stderr without changing the iteration's outcome. That
report is attributed only to the established session — a foreign, missing, or empty
session id names no task — and, once accepted, is delivered on every terminal path,
so a later contract failure cannot suppress a kill the CLI already reported.
_Avoid_: continuation turn (reserved for a task delivered while the turn is open),
dropped task

**Origin marker**:
The `parent_tool_use_id` field the Claude CLI puts on every assistant event —
null on the Backend's own message, the launching tool-use id on a background
subagent's — which the adapter reads to attribute a turn's response: only the
Backend's own messages assemble the answer and are scanned for completion and
needs-input markers, so a subagent speaking last is never mistaken for the answer.
Measured present on every one of 815 assistant events across five sessions, so
three shapes that never occurred are refused closed rather than credited: an event
that omits the marker entirely, a subagent event carrying a foreign session id, and
the background registration that licenses a second turn bearing a foreign id.
_Avoid_: subagent tag, parent id

**Interactive-only child**:
A child issue carrying the configured label (default `may-ask-owner`) that
embeds an owner decision and must be worked in an interactive session. Before the
first session, ralph resolves the concrete open issues carrying the label via
`gh` and injects their numbers into the prompt; the Loop protocol tells the
backend to treat them as blocked for an autonomous iteration and to halt for
input when only such children remain. The rule is advisory — ralph cannot observe
which child the backend selects — so only the resulting needs-input halt is
enforced.
_Avoid_: blocked issue (reserved for declared dependencies), operator label

**Run console**:
The module that owns every operator-facing line of a run — the header stating
the resolved settings and the evidence path, the Iteration rules and outcome
blocks, the terminal summary naming the git outcome on every path including
success, the Trust boundary line, the loud deviation warnings for a relaxed
guarantee, the full help block every failure gets once a run directory exists (the
`RALPH NEEDS OPERATOR` handoff, the backend-failure next step, and the budget-
exhaustion continuation command), and the bell — and the rendering apparatus behind
that small interface: the four-role palette, terminal and `NO_COLOR` detection,
dynamic width, no-wrap truncation, and the single choke point every operator-facing
string is redacted at. It is the only module permitted to write to a terminal, now
without exception — every emit site has migrated and the structural test enforces the
rule with no allowlist — and `cli` is the only module that constructs one, choosing
between its four renderings there and nowhere else. It owns two streams: the
dashboard on standard error, and the opt-in Backend feed on standard output, so
redirecting a transcript to a file leaves the dashboard on the terminal. The feed is
off by default, which is what makes the default view a dashboard rather than a feed;
quiet drops the status line and the Iteration blocks and nothing else, so an
unattended run never loses its header, its summary, or its help. It speaks for the
two commands that are not `run` as well: `clean` reports the runs it destroyed and
distinguishes that from having found nothing, so a command that irreversibly deletes
every run's evidence is never silent about it, and `resume` gets a compact header
naming the session being entered, the trust boundary re-proven, and the
host-isolation status — stated either way, never reported by omission — printed
before the process is replaced and there is nothing left to render.
_Avoid_: log, output, UI, stderr (the stream it happens to write to)

**Observation**:
A single fact a Backend adapter emits about a running Iteration through the
narrow one-method sink the Loop injects into `execute_iteration`, over a closed
set of frozen value types — the orchestrator's tool use and live context gauge,
the live subagent roster, the Stage the backend declared, the mid-run warnings the
adapter used to print itself, and
the Backend's own running commentary, each passage attributed to the Backend or to
the specific subagent that produced it. The adapter emits facts and the Run console
decides wording and rendering — including whether a fact is shown at all, which is
how the commentary left the default view without any adapter knowing it had — so
neither the Loop nor an adapter constructs operator-facing text; a new kind of
Observation is a new value type, never a wider interface, so the seam stays one
method while what rides it grows.
_Avoid_: event (reserved for a Backend's own stream events), log, message, progress bar

**Trust boundary**:
The set of properties ralph proves before spending budget: subscription-only
authentication, customization isolation, and host isolation. For a Claude session
it is re-proved on every `system/init` event — including a post-result teardown
init, which passes the identical proof but opens no turn — so a longer stream is a
stronger proof, never a weaker one, and stream position cannot weaken validation.
_Avoid_: security model, safety checks

**Host isolation**:
The OS-enforced confinement of a backend session so it cannot write outside
its sanctioned areas or read the operator's unrelated credentials. Defends
against accident, not malice.
_Avoid_: sandboxing (as a synonym for the general goal), containerization

**Sandbox**:
The concrete Seatbelt (`sandbox-exec`) profile ralph generates at runtime and
wraps a backend session in to enforce host isolation.
_Avoid_: container, VM, jail

**Accident**:
The adversary the sandbox defends against — a well-intentioned backend doing
something wrong (destructive command, credential read). Explicitly out of
scope: malicious code that attacks the sandbox itself.
_Avoid_: attack, exploit, threat (unqualified)

**In-scope credential**:
A credential the loop needs to do its job (the backend's own subscription
token, `gh`'s GitHub access — including `~/Library/Keychains/login.keychain-db`,
the one keychain carved out of the keychain denial because `gh` keeps its token
there) and therefore cannot be protected by the sandbox.
_Avoid_: allowed secret

**Profile template**:
The universal, operator-independent Seatbelt policy in tracked source (write
allow-list, read deny-list). Ralph fills it with runtime absolute paths to
produce the concrete profile; the filled-in profile lives only under the
untracked `.git/ralph/`, never in tracked source.
_Avoid_: config, ruleset

**Sandbox self-test**:
A one-shot probe ralph runs inside the generated profile before spending
budget, proving that a denied read and a denied write actually fail — turning
"we think it's sandboxed" into observed refusal.
_Avoid_: sandbox check, smoke test

**Handed-off session**:
A backend session that stopped the loop (question, timeout, interruption,
failure) and is offered for manual `ralph resume` recovery under the same
trust boundary.
_Avoid_: crashed session, failed run
