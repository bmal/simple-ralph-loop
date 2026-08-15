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

**Loop protocol**:
The contract ralph appends to every prompt telling the backend how to signal
an iteration's outcome (complete, or needs operator input) via markers ralph
can detect. Built once per run from the configured interactive-only label and
the concrete children ralph resolves as carrying it, so the same contract, its
resolved facts, and its marker parser stay in one module.
_Avoid_: prompt suffix, prompt template, system prompt

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
string is redacted at. It is the only module permitted to write to a terminal — the
emit sites still outside it are named explicitly by the structural test that enforces
the rule, and each is migrated in turn — and `cli` is the only module that constructs
one.
_Avoid_: log, output, UI, stderr (the stream it happens to write to)

**Observation**:
A single fact a Backend adapter emits about a running Iteration through the
narrow one-method sink the Loop injects into `execute_iteration`, over a closed
set of frozen value types — the orchestrator's tool use and live context gauge,
the live subagent roster, and the mid-run warnings the adapter used to print
itself. The adapter emits facts and the Run console decides wording and rendering,
so neither the Loop nor an adapter constructs operator-facing text; a new kind of
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
