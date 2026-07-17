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
can detect.
_Avoid_: prompt suffix, prompt template, system prompt

**Trust boundary**:
The set of properties ralph proves before spending budget: subscription-only
authentication, customization isolation, and host isolation.
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
token, `gh`'s GitHub access) and therefore cannot be protected by the sandbox.
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
