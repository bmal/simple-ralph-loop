---
Status: accepted
---

# Claude hook exclusion is proven at runtime, not by refusing the repository

## Context

Customization isolation, one of the three Trust boundary guarantees, requires
that external hooks are "excluded or cause a fail-closed preflight when exclusion
cannot be proven" (#3). Ralph did both. Every Claude session ran with
`disableAllHooks: true` in Ralph's own `--settings`, which is the exclusion. The
preflight also refused any repository whose `.claude/settings.json` carried a
`hooks` key or which had a `.claude/hooks` directory. That refusal was the
fail-closed fallback, because the exclusion could not be *proven*: the
`system/init` event re-proves MCP servers, plugins and tools on every turn, but
reports nothing about hooks. #16 then ratified that hooks "stay refused with and
without" `--unsafe-allow-agents`.

The refusal assumes that a repository which carries hooks is one Ralph should not
run. bmal/noteforge falsified that (commit `493b08c5`, #932). It commits a
`UserPromptSubmit` hook on purpose, for the people who use Claude Code in it. The
hook guards a typed `/<stage> <slug>` against pasting the bundle twice. It is not
Ralph's to remove, and Ralph has no need for it to run, since Ralph's prompt is
never a slash command. From that commit onwards every Ralph run against the
repository was refused before spending anything.

The refusal was also weaker than it looked. Claude Code loads hooks from sources
the static check never read: skill frontmatter, and project agent frontmatter
once the folder's workspace trust dialog has been accepted. noteforge has both
kinds of file and is trusted. So the refusal was neither sufficient for the
guarantee nor necessary for it.

### Measured on Claude Code 2.1.283, 2026-09-28

These probes used a throwaway repository and Ralph's exact argv (`-p
--setting-sources project --strict-mcp-config --settings <Ralph's settings>`).
Each session ran Bash, invoked a skill, and delegated to a subagent. Every case
had a positive control that set `disableAllHooks: false`, so a hook that stayed
silent under Ralph's settings is known to have been live.

| Hook source | Control (`false`) | Ralph's settings (`true`) |
|---|---|---|
| Project `.claude/settings.json`: `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `Stop` | all four fired | none fired |
| Skill frontmatter `hooks:` (`PreToolUse`) | fired | none fired |
| Hooks inside Ralph's own `--settings` JSON (the canary) | all four fired | none fired |
| Agent frontmatter `hooks:` on a CLI-defined agent (`--agents`): `PreToolUse`, `Stop` | both fired | none fired |
| Agent frontmatter `hooks:` on a *project* agent in an untrusted folder | never fired | none fired |

A project agent's frontmatter hooks run only in a folder whose workspace trust
dialog was accepted, and `-p` never accepts it. Trust is not inherited from a
trusted parent folder. So the trusted-project-agent path is not measured
directly. Its coverage is inferred from the CLI-defined agent, which uses the
same frontmatter mechanism and is exempt from the trust gate. One harness lesson
matters for the implementation: a hook runs with the session's cwd, so a marker
path must be absolute. An early probe gave a false "never fires" on a relative
path.

## Decision

- **K1: Repository hooks are admitted.** Preflight no longer refuses the
  `hooks` settings key or a `.claude/hooks` directory. Every other refusal
  stands, and no flag is added: the static refusal protected nothing that K2
  and K3 do not.
- **K2: Every Claude session runs with `disableAllHooks: true`.** This includes
  automated Iterations and `ralph resume`, as before. According to Claude Code's
  own documentation, a `--settings` value takes precedence over project and local
  settings, and `--setting-sources project` keeps user and local settings out
  entirely. Managed hooks, which `disableAllHooks` cannot switch off from here,
  stay refused by the existing managed-configuration checks.
- **K3: Every automated Iteration proves K2 held with a canary.** The Iteration's
  `--settings` register one hook on each measured event: `SessionStart`,
  `UserPromptSubmit`, `PreToolUse` with matcher `*`, and `Stop`. Each is an
  absolute `/usr/bin/touch` of its own file under the Iteration's run directory,
  `hook-canary/<Event>`. Host isolation already lets the session write there.
  With hooks off, the directory stays empty.
- **K4: A fired canary fails closed.** The canary directory is checked on every
  `system/init`, beside the rest of the Trust boundary, and again at end of
  stream before any outcome is judged. If any file is present, or the directory
  has gone missing, the Iteration ends as a `backend_contract_failure`. Once a
  session exists this is a resumable handoff, exactly like every other init-proof
  failure.
- **K5: `ralph resume` carries no canary.** Recovery replaces Ralph's process
  with an interactive session that Ralph no longer reads, so a canary there
  would be evidence nobody checks. Resume keeps K2.
- **K6: Re-measure on every Claude Code bump that Ralph adopts.** Before the
  version floor rises, or before a newer CLI is relied on, repeat the table above
  against the new version: each source with its control, and the canary. The
  per-Iteration canary catches a change to `disableAllHooks` itself. Only this
  re-measurement catches a *new hook source* that the switch does not cover.

This amends #3's acceptance criterion and #16's "hooks stay refused with and
without the flag". Hook exclusion stays a proven guarantee, but the proof has
moved from a static refusal of the repository to runtime evidence from each
session.

## Considered options

- **Keep the refusal, and remove or relocate the repository's hook when running
  Ralph** (for example, move it to `settings.local.json`). Rejected: it changes a
  repository Ralph has no business changing, and relocation dodges the check
  instead of meeting it.
- **A content-pinned allowlist.** The operator records a hash of the admitted
  hook set, and preflight admits only that exact content. Rejected: an admitted
  hook is still switched off only by `disableAllHooks`, so this relies on the
  same unproven exclusion as K1 and adds configuration to maintain for a hook
  that never runs. The canary is what closes the proof, whatever gets admitted.
- **`--allow-hooks`: an opt-in flag that admits hooks but keeps them off.**
  Rejected: it guards a refusal that K2 and K3 make redundant, and every run
  against such a repository, plus every recovery command, would have to carry it.
  The name would also mislead, because the hooks still do not run.
- **`--unsafe-allow-hooks`: an opt-in flag that lets the repository's hooks run.**
  Deferred until a repository needs its hooks live under Ralph. Such a flag ends
  per-session proof (a canary cannot distinguish admitted hooks from unexpected
  ones) and lets a hook inject context or block tools. Worse, a `UserPromptSubmit`
  hook could block Ralph's prompt, and a `Stop` hook returning `block` could keep
  a turn running past its result. If a repository ever needs it, it should record
  a hash of the hook set in the run evidence, warn as loudly as the other
  deviations, and be reproduced into recovery commands.

## Consequences

- **The guarantee is only as strong as the measured sources.** The canary proves
  per session that Ralph's own hook source is off. The K6 re-measurement is what
  covers project, skill and agent sources on each new version. A new hook source
  that `disableAllHooks` does not cover would pass the canary unnoticed until
  that re-measurement runs.
- **A fired canary is detected after the hook ran.** `SessionStart` runs before
  the first init can be checked. That is acceptable under ADR-0001's
  accident-not-malice scope, because the hook ran inside the same Seatbelt
  sandbox and cleaned environment as the session, and the Iteration is refused
  before it is judged.
- **Each Iteration's run directory gains an empty `hook-canary/`.** It is part
  of the retained evidence: empty means proven, and a file names the event that
  fired.
- **Managed hooks are unaffected.** `disableAllHooks` outside managed settings
  cannot switch them off, and Ralph already refuses to run under any managed
  Claude configuration.
