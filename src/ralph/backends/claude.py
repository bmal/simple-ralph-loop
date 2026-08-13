"""The Claude Backend adapter: preflight, customization refusal, Claude constants
and host paths, event accumulation, iteration, and session persistence.

Invariants:
- Every ``system/init`` re-proves the subscription-safe session: it must report
  ``apiKeySource == "none"`` (billing rides the proven pro/max OAuth login, not a
  metered key), ``bypassPermissions`` full-auto mode, no external MCP servers or
  plugins, a tool set that is a subset of ``CLAUDE_BUILTIN_TOOLS``, and the same
  session id — every time, so a longer stream is a stronger proof, not a weaker
  one. Anything else fails closed. The session id is checkpointed on the first
  init before the rest is validated so a later contract failure is still a
  resumable handoff. A *pre-result* init opens a turn on this proof; a *post-result*
  init passes the identical proof but opens no turn — it is teardown, not a new
  turn (see the shape bullet; #50 I1/I2). The single ``_prove_init`` runs both,
  so validation cannot weaken with stream position.
- The stream is read to EOF and may carry multiple turns. A second (or later) init
  is admitted only in a session that registered a background task
  (``background_tasks_changed``) while at least one turn was open and the
  registration carried the session's own id — no turn, no cause, and a foreign id
  is a crossed stream, so neither licenses anything; an unexplained duplicate init
  stays the hard ``Claude emitted duplicate initialization metadata`` failure it is
  today. Results are flushed at EOF in turn order — a ``result`` does *not* close a
  turn. The i-th result belongs to the i-th turn; Ralph asserts equal counts and
  that each result agrees with its turn's last message from the Backend itself,
  failing closed otherwise. The Iteration's outcome is judged on that final
  message. ``execute_iteration`` returns that final message alongside the outcome
  and session id, so the Run console can show it in the Iteration's outcome block;
  it is returned raw, and the console truncates it for display only (register G14).
- What may follow the results is a matter of *shape*, not a census of subtypes,
  applied uniformly at every stream position (H4/H5). Once results have begun,
  between them and after them alike, the violations are the events that actually
  change turn attribution: an ``assistant`` message extends a turn, and a
  ``result`` beyond the turn count flushes a turn that does not exist — each fails
  closed. A post-result ``init`` is *not*, on its own, one of them: Claude Code
  2.1.228 emits one at teardown whenever a session parked a background task (the
  observed park tail is a drained ``background_tasks_changed``, ``task_updated``,
  ``task_notification``, then a re-``init``), and that trailing init opens no
  turn — nothing follows it — so failing on it closed every background-using
  Iteration (#47). It is still validated exactly as any init: ``_prove_init``
  re-proves the established session and the full Trust boundary at this position
  too, so a crossed session id or unsafe metadata fails closed rather than
  slipping in under teardown tolerance (#50 I1/I2). Once valid it opens no turn,
  only remembered, and *proves* the per-turn-flush shape only if an ``assistant``
  or a further ``result`` then follows it, closing a real continuation turn the
  parser cannot place; that follow-up is what fails closed, and it alone names the
  per-turn-flush cause (H6/I3). Every other ``system`` subtype is teardown or
  telemetry (``task_summary`` and the rest of an open, growing
  vocabulary); it changes no attribution and is ignored, whether or not Ralph
  recognises it, so a later CLI addition is not another outage. A bare post-result
  assistant or a duplicate result keeps the generic wording.
- Marker semantics are per *message*, not per turn: the message the Iteration is
  judged on decides completion and the needs-input halt, and a needs-input marker
  in any other message of the Backend's own — an earlier turn's or an earlier
  message of the final turn's — is one the Backend withdrew, so it warns on stderr
  and continues rather than costing the Iteration. That withdrawn-marker warning and
  the unmarked-question warning stay mid-run stderr lines, but the fragment each
  quotes back is redacted and then bounded through ``protocol.bounded_quote`` so a
  warning is a bounded interruption, never the Backend's whole final message (#39).
- The composed prompt carries an adapter-local ``BACKGROUND_TASK_DIRECTIVE`` after
  the shared Loop protocol (H3): the Backend may launch background work but must not
  *park* on it -- end its turn while a task is still unresolved, expecting to be
  re-invoked when it completes. It cannot bank on that: an unresolved task has two
  observed outcomes and no guarantee of either -- teardown may kill it, or it may
  finish and reappear as a fresh continuation Ralph cannot attribute to the
  ended turn (the trailing teardown ``init`` #51 validates without opening a turn) --
  so the directive names both and tells the Backend not to wait on a later delivery,
  rather than claiming the task is always killed and the notification never arrives
  (#50 I5). The directive is Claude-adapter-local because only Claude Code has a
  background-task runtime and the Loop protocol is outcome signalling only; the
  OpenCode adapter never appends it. Prevention is the mechanism (H2): the directive
  forbids abandoning, never launching.
- When the CLI's teardown explicitly reports it killed a background task (a
  ``system/task_updated`` whose ``patch`` marks the task ``killed``), the adapter
  names the abandoned task on stderr, the stream the operator already watches for
  the mid-run marker warnings (H9). Detection is that explicit report alone, never
  inference from a task-list drain, which also fires mid-stream on an ordinary
  completion (H7). The observation must also carry the *established* session id: a
  foreign, missing, or empty id is a crossed stream and names no task, the same
  class #49 tightened for assistant and licensing events (#50 I6); a duplicate
  report of a task already recorded fabricates no second one. Once accepted, the
  warning is delivered on every terminal path -- an ordinary completion and a
  contract-failure handoff alike -- so a halt never hides the work the CLI said it
  abandoned (#50 I7). The report is observational: it leaves the Iteration's
  outcome and final message unchanged (H1). It is a not-yet-migrated operator-facing
  write registered as migration debt for the Run console program (register G13/G14;
  the terminal-ownership test names it and #36 is told to adopt it).
- ``preflight`` returns a ``console.Deviation`` (or ``None``): when it admits the
  ``.claude/agents`` vector under ``--unsafe-allow-agents`` and otherwise clears, it
  hands back the fact so the caller states the relaxed subagent-isolation guarantee
  loudly through the Run console — the adapter words no operator-facing warning of its
  own (register G7/G14). A refused run is not warned about isolation it never reaches.
- ``parent_tool_use_id`` is the origin marker, present on every assistant event
  (null on the Backend's own, the launching tool-use id on a background subagent's).
  It distinguishes the Backend's own messages from a subagent's: only the Backend's
  own messages assemble a turn's response and are scanned for completion/needs-input
  markers, so a subagent speaking last is never mistaken for the answer; the
  subagent's messages still stay in the retained stream evidence. The native question
  tool halts wherever it appears, unfiltered by origin. Three shapes are refused
  closed rather than credited, each licensed by five observed sessions that never
  showed them (#49): an assistant event that *omits* the marker entirely (it would
  default to the Backend and let a subagent assemble the answer); a subagent
  assistant event whose ``session_id`` is not the parent session's (a crossed
  stream — enforced for every assistant event by the same ``_require_session`` guard
  the Backend's own events pass); and, above, a ``background_tasks_changed`` bearing
  a foreign id, which cannot open the second-turn relaxation.
- ``--unsafe-allow-agents`` relaxes only the agent vectors (``.claude/agents`` and
  the settings ``agent`` key). Managed, server-managed, hooks, plugins, and every
  other unsafe settings key stay refused and are checked *before* the local
  customization gather, so the opt-out hint is advertised only when an agent vector
  is the sole blocker and never masquerades as a remedy for something it cannot fix.
- A stop Ralph itself caused (timeout/interrupt) is classified *before* any contract
  failure, so an interrupted session's error result is never misread as misbehavior.
- A wedged background subagent that never drains is bounded only by ``--timeout``;
  the adapter adds no separate idle detection.

Depends on / must not know: ``environment`` (the sanitized base and the timeout
ceiling its ``environment`` layers on), ``errors``, ``launch`` (``session_argv``),
``process``, ``protocol``, ``redaction`` (functions only), ``gitcontext``,
``preflight``, and ``console`` (only for the ``Deviation`` value type ``preflight``
returns — never a console instance). It must not know how the Loop schedules
Iterations; the Loop must not know these helpers exist beyond the five Backend
interface names.

See also: ``backends`` (registry and the five-name Protocol), ``backends.opencode``
(twin adapter), ``launch`` (``session_argv``, the wrapped argv), ``protocol``
(marker detection and ``active_protocol``, the run's protocol text appended here).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any

from ..console import CLAUDE_AGENTS_DEVIATION, Deviation
from ..environment import BACKEND_TIMEOUT_MS, clean_environment
from ..errors import (
    HandoffError,
    RalphError,
    raise_backend_contract_failure,
)
from ..gitcontext import command, write_json
from ..launch import session_argv
from ..preflight import common_preflight, version_tuple
from ..process import ProcessController, raise_if_controlled_stop
from ..protocol import (
    active_protocol,
    bounded_quote,
    explicit_needs_input,
    extract_question,
    has_completion_marker,
    inferred_needs_input,
)
from ..redaction import redact


MIN_CLAUDE_VERSION = (2, 1, 208)
# Host locations consulted to detect MDM-managed Claude configuration. Both are
# absolute system paths in production; dedicated test seams (see
# claude_managed_root / claude_profiles_executable) let the suite isolate the
# checks from real host state without weakening the production defaults.
DEFAULT_CLAUDE_MANAGED_ROOT = "/Library/Application Support/ClaudeCode"
DEFAULT_CLAUDE_PROFILES = "/usr/bin/profiles"
# Built-in tool names a subscription Claude Code >= 2.1.208 session may report
# in its init event (observed against 2.1.224). MCP tools are namespaced
# `mcp__server__tool` and plugin tools carry their own names, so anything
# outside this set still fails the subset assertion closed. A Claude Code
# upgrade that ships a new built-in halts every run until its name is added
# here, so keep this set current with the newest supported CLI.
CLAUDE_BUILTIN_TOOLS = {
    "Agent",
    "Artifact",
    "AskUserQuestion",
    "Bash",
    "CronCreate",
    "CronDelete",
    "CronList",
    "DesignSync",
    "Edit",
    "EnterPlanMode",
    "EnterWorktree",
    "ExitPlanMode",
    "ExitWorktree",
    "Glob",
    "Grep",
    "LSP",
    "ListAgents",
    "Monitor",
    "NotebookEdit",
    "PushNotification",
    "Read",
    "RemoteTrigger",
    "ReportFindings",
    "ScheduleWakeup",
    "SendMessage",
    "Skill",
    "Task",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskOutput",
    "TaskStop",
    "TaskUpdate",
    "TodoWrite",
    "ToolSearch",
    "WebFetch",
    "WebSearch",
    "Workflow",
    "Write",
}
CLAUDE_SETTINGS = json.dumps(
    {
        "autoMemoryEnabled": False,
        "disableAllHooks": True,
        "disableClaudeAiConnectors": True,
    },
    separators=(",", ":"),
)
CLAUDE_CUSTOMIZATION_DIRS = ("agents", "hooks", "plugins")
# Settings keys that, if present in `.claude/settings.json`, defeat the proof of
# safe isolation. Only `agent` is relaxable via --unsafe-allow-agents.
UNSAFE_CLAUDE_SETTINGS_KEYS = frozenset(
    {
        "agent",
        "apiKeyHelper",
        "awsAuthRefresh",
        "awsCredentialExport",
        "enabledPlugins",
        "env",
        "extraKnownMarketplaces",
        "hooks",
    }
)
CUSTOMIZATION_REFUSAL = "Claude customizations must be disabled before running Ralph"
# Appended to the refusal only when a Claude agent vector — the `.claude/agents`
# directory or the settings.json `agent` key — is the *sole* blocker, so the
# operator can discover the supported opt-out from the failure itself. It is
# withheld from every other refusal (a hooks/plugins directory, managed or
# server-managed configuration, or any other unsafe settings key, including when
# `agent` appears alongside one) because the flag cannot relax those and must
# never be advertised as a false remedy.
AGENT_OPT_OUT_HINT = (
    "; a Claude agent vector is the only blocker, so you may re-run with "
    "--unsafe-allow-agents to admit the .claude/agents directory and the "
    "settings.json 'agent' key for this run (unsafe: Ralph then cannot prove "
    "Claude subagent isolation)"
)
# Appended by the Claude adapter alone to every composed prompt (H3): it is
# adapter-local, not part of the shared Loop protocol, because only Claude Code has
# a background-task runtime and the Loop protocol is outcome signalling only. A
# Backend may launch background work but must not *park* on it -- end its turn while
# a task is still unresolved, expecting to be re-invoked when it completes. What
# becomes of an unresolved task once the Backend stops has two observed outcomes and
# no guarantee of either: teardown may kill it (the CLI's own ``task_updated``/
# ``killed`` report), or it may finish and reappear as a fresh continuation Ralph
# cannot attribute to the ended turn (the trailing teardown ``init`` the #47 H10
# live-smoke tail carries, which #51 now validates without opening a turn). Neither
# is a delivery the Backend can wait for, so the directive names both outcomes and
# tells the Backend not to rely on a later notification, rather than the disproved
# categorical "the task is killed and the notification never arrives" -- a directive
# that misstates the runtime is worse than none (#50 I5). The observed failure had the
# Backend explicitly reasoning it would wait for that notification. The directive
# forbids abandoning, never launching (#48).
BACKGROUND_TASK_DIRECTIVE = (
    "\n\nBackground work must be resolved inside the turn that launched it. You may "
    "launch background tasks where they help (parallel review, a long survey), but "
    "you must not end your turn while one is still unresolved. What becomes of an "
    "unresolved task once you stop is not yours to count on: the session may tear "
    "down and kill it, or it may finish and reappear as a fresh continuation Ralph "
    "cannot attribute to your turn. Neither outcome is a delivery you can wait "
    "for, so do not stop expecting a later notification to resume the work. Before "
    "you stop, either bring the work back into the turn (wait for its result and act "
    "on it) or cancel it and say what you left unverified, so the next iteration can "
    "pick it up deliberately."
)


def validate_model(model: str) -> None:
    if not model.startswith("claude-"):
        raise RalphError("model must be a Claude subscription model")


def environment(model: str) -> dict[str, str]:
    # The sanitized base plus Claude's request and Bash-tool timeout ceilings and
    # autoupdater suppression. The timeouts are pinned to the 32-bit ceiling so
    # Ralph's own iteration timer always stays authoritative; the model is not
    # consulted (Claude routing rides the proven subscription OAuth, not env).
    ceiling = str(BACKEND_TIMEOUT_MS)
    env = clean_environment()
    env.update(
        {
            "API_TIMEOUT_MS": ceiling,
            "BASH_DEFAULT_TIMEOUT_MS": ceiling,
            "BASH_MAX_TIMEOUT_MS": ceiling,
            "DISABLE_AUTOUPDATER": "1",
        }
    )
    return env


def claude_managed_root() -> Path:
    # System directory holding MDM-managed Claude configuration. Its default is
    # an absolute macOS path; RALPH_CLAUDE_MANAGED_ROOT is honored only as a test
    # seam so the suite can point the managed-config check at an isolated,
    # host-independent location. Production runs never set it.
    return Path(os.environ.get("RALPH_CLAUDE_MANAGED_ROOT") or DEFAULT_CLAUDE_MANAGED_ROOT)


def claude_profiles_executable() -> str:
    # Absolute path to the macOS `profiles` tool used to detect MDM-managed
    # Claude preferences. RALPH_CLAUDE_PROFILES is honored only as a test seam so
    # the suite does not depend on the host's real configuration profiles;
    # production runs never set it and always use the system binary.
    return os.environ.get("RALPH_CLAUDE_PROFILES") or DEFAULT_CLAUDE_PROFILES


def read_unsafe_settings_keys(settings_path: Path) -> set[str]:
    # Return the unsafe keys present in `.claude/settings.json`, or an empty set
    # when the file is absent. Malformed settings fail closed with their own
    # message rather than being treated as carrying no unsafe keys.
    if not settings_path.exists():
        return set()
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise RalphError("Claude project settings are malformed") from None
    if not isinstance(settings, dict):
        raise RalphError("Claude project settings are malformed")
    return set(UNSAFE_CLAUDE_SETTINGS_KEYS.intersection(settings))


def reject_claude_customizations(worktree: Path, allow_agents: bool = False) -> Deviation | None:
    claude_dir = worktree / ".claude"
    # --unsafe-allow-agents relaxes only the agent vectors: the
    # `.claude/agents` directory and the settings.json `agent` key. It exists for
    # repos whose loop develops or depends on subagents. Hooks, plugins, managed
    # configuration, and every other unsafe setting stay refused, and runtime
    # MCP/plugin/tool isolation is still proven from the init event. The trade is
    # deliberate and unsafe: Ralph can no longer prove which subagents loaded, so
    # the operator vouches for them for this run. That relaxed guarantee is stated
    # loudly by the Run console (register G7/G14), so this returns the deviation
    # rather than wording it -- and only when the check otherwise clears, so a run
    # that is refused anyway is not warned about the isolation it never reaches.
    admitted_agents = allow_agents and (claude_dir / "agents").exists()
    # Managed and server-managed configuration is refused before the local
    # customization checks: the flag cannot relax it, so it must take precedence
    # over any co-present agent vector and never masquerade as something the
    # opt-out could fix.
    managed_root = claude_managed_root()
    if any(
        path.exists()
        for path in (
            managed_root / "managed-settings.json",
            managed_root / "managed-settings.d",
            managed_root / "managed-mcp.json",
        )
    ):
        raise RalphError("managed Claude configuration prevents proving safe isolation")
    managed_preferences = command(
        [claude_profiles_executable(), "show", "-type", "configuration"], allow_failure=True
    )
    if managed_preferences.returncode:
        raise RalphError("could not inspect managed Claude preferences")
    if "com.anthropic.claudecode" in managed_preferences.stdout:
        raise RalphError("managed Claude preferences prevent proving safe isolation")
    if (Path.home() / ".claude" / "remote-settings.json").exists():
        raise RalphError("server-managed Claude settings prevent proving safe isolation")
    # Gather every local customization blocker before refusing so an
    # agent-vector-only refusal can be told apart from one that also (or instead)
    # trips a vector the flag cannot relax. When the flag is set the agent
    # vectors are admitted and so are excluded from the offending sets.
    relaxable_dirs = {"agents"} if allow_agents else set()
    relaxable_keys = {"agent"} if allow_agents else set()
    offending_dirs = [
        name
        for name in CLAUDE_CUSTOMIZATION_DIRS
        if name not in relaxable_dirs and (claude_dir / name).exists()
    ]
    offending_keys = read_unsafe_settings_keys(claude_dir / "settings.json") - relaxable_keys
    if not offending_dirs and not offending_keys:
        return Deviation(CLAUDE_AGENTS_DEVIATION) if admitted_agents else None
    # The hint is offered only when every offending item is an agent vector. If
    # the flag is already set the agent vectors are filtered out above, so any
    # surviving blocker is non-agent and the plain refusal stands.
    agent_blocker = "agents" in offending_dirs or "agent" in offending_keys
    non_agent_blocker = bool(
        [name for name in offending_dirs if name != "agents"]
        + [key for key in offending_keys if key != "agent"]
    )
    hint = agent_blocker and not non_agent_blocker
    raise RalphError(CUSTOMIZATION_REFUSAL + (AGENT_OPT_OUT_HINT if hint else ""))


def preflight(
    worktree: Path, slug: str, model: str, env: dict[str, str], allow_agents: bool = False
) -> Deviation | None:
    common_preflight(worktree, slug, "claude", env)
    agent_deviation = reject_claude_customizations(worktree, allow_agents)
    version = command(["claude", "--version"], cwd=worktree, env=env).stdout
    if version_tuple(version, "Claude Code") < MIN_CLAUDE_VERSION:
        raise RalphError("Claude Code 2.1.208 or newer is required")
    status_text = command(["claude", "auth", "status"], cwd=worktree, env=env).stdout
    try:
        status = json.loads(status_text)
    except json.JSONDecodeError:
        raise RalphError("Claude authentication status is malformed") from None
    subscription_types = {"pro", "max"}
    stored_subscription = (
        isinstance(status, dict)
        and status.get("loggedIn") is True
        and status.get("authMethod") == "claude.ai"
        and status.get("apiProvider") == "firstParty"
        and status.get("subscriptionType") in subscription_types
    )
    setup_token = (
        bool(env.get("CLAUDE_CODE_OAUTH_TOKEN"))
        and isinstance(status, dict)
        and status.get("loggedIn") is True
        and status.get("apiProvider") == "firstParty"
        and status.get("authMethod") in {"claude.ai", "oauth"}
        and status.get("subscriptionType") in subscription_types
    )
    if not stored_subscription and not setup_token:
        raise RalphError("Claude must use first-party subscription OAuth authentication")
    # Every proof cleared: hand back the admitted-agents deviation, if any, for the
    # caller to state loudly through the Run console (register G7/G14).
    return agent_deviation


def synthetic_error_reason(event: dict[str, Any], message: dict[str, Any]) -> str:
    # Turn a Claude Code synthetic error message into an operator-facing halt
    # reason. The human-readable error text lives in the message content; the
    # machine-readable cause lives in the event's top-level `error` field.
    content = message.get("content")
    parts = content if isinstance(content, list) else []
    detail = " ".join(
        part["text"]
        for part in parts
        if isinstance(part, dict)
        and part.get("type") == "text"
        and isinstance(part.get("text"), str)
    ).strip()
    if event.get("error") == "authentication_failed" or "authenticat" in detail.lower():
        # A revoked/expired subscription token is the recoverable case: tell the
        # operator exactly how to re-authenticate. The printed handoff already
        # follows with the manual-resume command for this same session.
        return (
            "Claude authentication failed (subscription OAuth token was revoked or "
            "expired); re-authenticate by running `claude` and using /login, then resume"
        )
    if detail:
        return f"Claude returned a synthetic error instead of a model response: {detail}"
    return "Claude returned a synthetic error instead of a model response"


def event_after_result_reason(per_turn_flush: bool) -> str:
    # Name the cause an operator can act on. F2's "results are flushed at EOF, in
    # turn order" is the observation the whole turn attribution rests on, and it
    # was taken from one Claude Code build. The per-turn-flush wording is reserved
    # for the shape that evidences that build (H6): a post-result *init* that a
    # turn actually *follows* -- an assistant or a further result that closes a
    # continuation turn Ralph cannot place. A lone post-result init is teardown
    # (2.1.228 emits one after a parked background task) and never reaches here;
    # only the follow-up does, and it is what proves the CLI is closing each turn
    # with its own result instead of flushing them together at EOF. That is a
    # stream-shape change, not a misbehaving Backend, and blaming the Backend
    # points the operator away from the fix. A bare post-result assistant or a
    # result beyond the turn count -- no post-result init before it -- is an
    # ordinary contract violation and keeps the generic wording.
    if per_turn_flush:
        return (
            "Claude continued the session after a result, so this Claude Code build "
            "flushes a result per turn instead of at end of stream; Ralph cannot "
            "attribute turns in that shape"
        )
    return "Claude emitted an event after the terminal result"


def killed_task_label(task_id: str | None) -> str:
    # Name an abandoned background task for the operator by the one identifier the
    # observed killed-task event carries: the top-level ``task_id`` (the ``patch``
    # holds only ``status`` and ``end_time``, so no human description is available
    # there -- H12). The id is CLI-generated structural metadata, printed as-is like
    # the session id in a resume command; the task's full detail stays in the
    # retained stream. A report with no id still names that something was killed
    # rather than staying silent.
    return f"task {task_id}" if task_id else "an unnamed background task"


class ClaudeTurn:
    # One turn of a Claude session: the model its init reported, and the text of
    # each of the Backend's own (non-subagent) assistant messages in order. The
    # turn's answer is its last such message; a background subagent's messages are
    # recorded elsewhere and never assemble the turn's response.
    def __init__(self, model: str) -> None:
        self.initial_model = model
        self.parent_texts: list[str] = []


class ClaudeEventResult:
    def __init__(self, model: str) -> None:
        self.expected_model = model
        self.session_id: str | None = None
        self.turns: list[ClaudeTurn] = []
        self.background_seen = False
        self.post_result_init = False
        self.assistant_models: list[str] = []
        self.results: list[str] = []
        self.model_usage: list[str] = []
        self.question: str | None = None
        self.killed_tasks: list[str | None] = []

    @property
    def initial_model(self) -> str | None:
        return self.turns[0].initial_model if self.turns else None

    @property
    def final_text(self) -> str | None:
        # The Iteration's answer is the final turn's result; results are flushed
        # at EOF in turn order, so the last one is the final turn's.
        return self.results[-1] if self.results else None

    @property
    def superseded_texts(self) -> list[str]:
        # Every message the Backend itself sent except the one the Iteration is
        # judged on -- its last message of the final turn, which the F3 agreement
        # check ties to `final_text`. Flattening the turns is what makes a marker
        # the Backend spoke past *within* a turn as visible as one it spoke past
        # across turns; both are the same withdrawal.
        spoken = [text for turn in self.turns for text in turn.parent_texts]
        return spoken[:-1]

    def accept(self, event: Any) -> None:
        if not isinstance(event, dict):
            # A non-dict line carries no turn structure and can change no turn
            # attribution, so it is ignored at every stream position.
            return
        event_type = event.get("type")
        subtype = event.get("subtype")
        if event_type == "system" and subtype == "task_updated":
            # Consider an explicit killed-task report wherever it appears (H7): it is
            # observational, changes no turn attribution, and is warned about after
            # the Iteration is judged. `_note_killed_task` records it only for the
            # established session (I6); noting it before the post-result shape gate
            # keeps detection uniform at every stream position.
            self._note_killed_task(event)
        if self.results:
            # Shape-based rule (H4/H5), applied uniformly once results have begun
            # -- between them and after them alike, so the inter-result window is
            # no longer a separate, untested regime. The only violations are the
            # events that actually change turn attribution: an assistant message
            # (it extends a turn) and a result beyond the turn count (a flush for
            # a turn that does not exist). A post-result init is *not* one of them
            # on its own -- Claude Code 2.1.228 emits one at teardown after a
            # parked background task, and that trailing init opens no turn, so
            # rejecting it closed every background-using Iteration (#47). It is
            # still re-proved: `_prove_init` re-checks the established session and
            # the full Trust boundary at this position too (I1/I2), so a crossed
            # session id or unsafe metadata fails closed here rather than slipping
            # in as teardown. Once valid it opens no turn, only remembered: it
            # proves the per-turn-flush shape solely if an assistant or a further
            # result then follows it, closing a real continuation turn Ralph
            # cannot place -- and that follow-up is what fails closed, with the
            # wording reserved for it (H6/I3). A lone trailing init, or one
            # followed only by more telemetry, is teardown and passes. Every other
            # `system` subtype is teardown or telemetry, ignored whether or not
            # Ralph recognises it, so a later CLI addition is not another outage. A
            # result while results are still outstanding is the ordinary positional
            # flush and passes through to be attributed below.
            results_complete = len(self.results) >= len(self.turns)
            if event_type == "system" and subtype == "init":
                self._prove_init(event, opening_turn=False)
                self.post_result_init = True
                return
            if event_type == "assistant" or (event_type == "result" and results_complete):
                raise RalphError(event_after_result_reason(self.post_result_init))
            if event_type != "result":
                return
        if event_type == "system" and subtype == "background_tasks_changed":
            # A subagent launched with run_in_background finishes asynchronously,
            # after the turn that launched it, and forces the CLI to open a second
            # turn with a fresh `system` init. That registration is what licenses
            # the later init: a duplicate init is admitted only in a session that
            # recorded a background task here, and an unexplained one still fails
            # closed. Synchronous subagents return inline as a tool result and
            # never emit this, so they never flip the gate. Neither does a
            # registration arriving before the first init: F1 licenses the
            # relaxation by observed *cause*, and no turn existed yet to launch
            # anything, so such an event explains no later init. It licenses only
            # when it carries the session's *own* id -- every one of the 10
            # licensing events measured did, so an event bearing a foreign id is a
            # crossed stream that must not open the multi-turn relaxation (#49).
            if self.turns and event.get("session_id") == self.session_id:
                self.background_seen = True
            return
        if event_type == "system" and subtype == "init":
            self._accept_init(event)
            return
        if event_type == "assistant":
            self._accept_assistant(event)
            return
        if event_type == "result":
            self._accept_result(event)

    def _accept_init(self, event: dict[str, Any]) -> None:
        # A pre-result init opens a turn: it re-proves the Trust boundary and then
        # appends the turn. A post-result init never reaches here -- `accept`
        # routes it to `_prove_init(opening_turn=False)`, which re-proves the
        # identical boundary but opens no turn (I1/I2).
        model = self._prove_init(event, opening_turn=True)
        self.turns.append(ClaudeTurn(model))

    def _prove_init(self, event: dict[str, Any], *, opening_turn: bool) -> str:
        # Re-prove the established session and the full Trust boundary for an
        # initialization event, returning the model it reported. Applied
        # uniformly to every init at every stream position (I1/I2): the same
        # proof a turn-opening init passes is required of a post-result init that
        # is only tolerated as teardown, so a crossed session id or unsafe
        # isolation metadata fails closed wherever the init appears. `opening_turn`
        # gates only the duplicate-init rule -- the question of whether *this* init
        # may open a new turn -- which a post-result init, opening none, never
        # poses.
        session_id = event.get("session_id")
        if isinstance(session_id, str) and session_id:
            # Checkpoint the session id before validating the rest of the event so
            # a later contract failure in a partially malformed init is still a
            # consuming, resumable handoff rather than an unrecoverable failure.
            # Every init must carry the same id.
            if self.session_id is not None and self.session_id != session_id:
                raise RalphError("Claude stream contained inconsistent session metadata")
            self.session_id = session_id
        model = event.get("model")
        if not isinstance(session_id, str) or not session_id or not isinstance(model, str):
            raise RalphError("Claude initialization omitted required metadata")
        if opening_turn and self.turns and not self.background_seen:
            # A second (or later) turn-opening init is admitted only in a session
            # that registered a background task; otherwise it is the unexplained
            # duplicate init that has always failed closed.
            raise RalphError("Claude emitted duplicate initialization metadata")
        if model != self.expected_model:
            raise RalphError("Claude initial model did not match the selected model")
        # Every init re-proves the full Trust boundary, so a longer stream is a
        # stronger proof, not a weaker one. `apiKeySource` "none" means no metered
        # API key is in play, so billing rides the OAuth login preflight proved is
        # a pro/max subscription; `bypassPermissions` is full-auto mode; the MCP,
        # plugin, and tool sets prove no external customization loaded. Any other
        # value fails closed -- on the first turn, on every turn after it, and on a
        # post-result teardown init alike.
        if event.get("apiKeySource") != "none":
            raise RalphError("Claude session did not use subscription OAuth")
        if event.get("permissionMode") != "bypassPermissions":
            raise RalphError("Claude session did not enter full-auto permission mode")
        if event.get("mcp_servers") != [] or event.get("plugins") != []:
            raise RalphError("Claude loaded external MCP servers or plugins")
        tools = event.get("tools")
        if (
            not isinstance(tools, list)
            or any(not isinstance(tool, str) for tool in tools)
            or not set(tools).issubset(CLAUDE_BUILTIN_TOOLS)
        ):
            raise RalphError("Claude loaded an unknown or external tool")
        return model

    def _accept_assistant(self, event: dict[str, Any]) -> None:
        self._require_session(event.get("session_id"))
        if "parent_tool_use_id" not in event:
            # The origin marker distinguishes the Backend's own messages from a
            # subagent's, and it was present on every one of the 815 assistant
            # events measured across five sessions -- null on the Backend's own,
            # an identifier on a subagent's. An event that omits it entirely gives
            # no origin to read, so `.get()` would silently default it to the
            # Backend and let a subagent's words assemble the Iteration's answer.
            # Refuse it rather than guess (register H, #49).
            raise RalphError("Claude assistant event omitted its origin marker")
        message = event.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("model"), str):
            raise RalphError("Claude assistant event omitted required metadata")
        model = message["model"]
        if not model.startswith("claude-"):
            # Claude Code carries a hard API failure in a synthetic assistant
            # message (model "<synthetic>") instead of a real model turn. The
            # common case is a revoked or expired subscription OAuth token -- a
            # 401 authentication_failed that only surfaces here, after preflight
            # already passed -- so name that cause and the one-line fix rather
            # than the misleading "model fallback" text. Any other synthetic
            # error still fails closed, just with its own message attached.
            if model == "<synthetic>":
                raise RalphError(synthetic_error_reason(event, message))
            raise RalphError("Claude used a non-subscription model fallback")
        self.assistant_models.append(model)
        content = message.get("content")
        if not isinstance(content, list):
            raise RalphError("Claude assistant event omitted content")
        # Only the Backend's own text (a null origin marker, refused above if the
        # marker is absent) assembles the turn's response and is later scanned for
        # markers, so a subagent speaking last is never mistaken for the answer;
        # the subagent's output is still printed and retained as evidence.
        is_backend = event.get("parent_tool_use_id") is None
        # Each Claude stream-json assistant event carries a complete message
        # (there are no incremental text deltas without partial-message mode),
        # so print each part on its own line: text as a paragraph, tool use as a
        # bracketed progress marker matching the OpenCode backend's style.
        # Printing with end="" here would glue consecutive messages together.
        texts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
                if part["text"]:
                    print(redact(part["text"]), flush=True)
                continue
            if part.get("type") == "tool_use":
                name = part.get("name")
                if name == "AskUserQuestion":
                    # The native question tool halts wherever it appears, whether
                    # the Backend or a subagent reached for it.
                    self.question = extract_question(part.get("input")) or "Claude attempted to ask a question."
                print(redact(f"[{name if isinstance(name, str) and name else 'tool'}]"), flush=True)
        text = "".join(texts)
        if text and is_backend and self.turns:
            self.turns[-1].parent_texts.append(text)

    def _accept_result(self, event: dict[str, Any]) -> None:
        self._require_session(event.get("session_id"))
        if event.get("subtype") != "success" or event.get("is_error") is not False:
            raise RalphError("Claude session reported an unsuccessful result")
        result = event.get("result")
        if not isinstance(result, str) or not result:
            raise RalphError("Claude result omitted the final assistant response")
        # Results are attributed positionally: the i-th result belongs to the
        # i-th turn. `accept` admits a result only while results are still
        # outstanding, and a result cannot arrive before its turn's init
        # established the session, so this index is always in range.
        turn = self.turns[len(self.results)]
        # Every turn's result must be backed by one of the Backend's own messages
        # to agree with; a turn that produced only subagent output leaves nothing
        # to verify the attribution against, so fail closed rather than trust an
        # unbacked result.
        if not turn.parent_texts:
            raise RalphError("Claude produced a result for a turn with no assistant response")
        # The i-th result must agree with the i-th turn's last Backend message so
        # a contradictory result -- a different final answer than the one that was
        # streamed, or one that echoes a subagent instead of the Backend -- fails
        # closed rather than being trusted.
        if result.strip() != turn.parent_texts[-1].strip():
            raise RalphError("Claude terminal result disagreed with the final assistant response")
        usage = event.get("modelUsage")
        if not isinstance(usage, dict) or any(
            not isinstance(model, str) or not model.startswith("claude-") for model in usage
        ):
            raise RalphError("Claude result omitted valid model usage")
        # modelUsage is unioned across every turn's result; each model named must
        # still be a Claude model.
        for model in usage:
            if model not in self.model_usage:
                self.model_usage.append(model)
        self.results.append(result)

    def _belongs_to_session(self, value: Any) -> bool:
        # An id belongs to the established session only once an init has set the
        # session id and the value matches it. A missing, empty, or foreign id is a
        # crossed stream. The single predicate keeps the session gate identical
        # wherever it is applied -- a raising caller and an observational one alike.
        return self.session_id is not None and value == self.session_id

    def _require_session(self, value: Any) -> None:
        if not self._belongs_to_session(value):
            raise RalphError("Claude stream contained inconsistent session metadata")

    def _note_killed_task(self, event: dict[str, Any]) -> None:
        # Park detection uses the CLI's explicit killed-task report -- a
        # `task_updated` whose patch marks the task `killed` -- never inference from
        # a task-list drain, which also fires mid-stream on an ordinary completion
        # and would misreport it (H7). The report is observational (H1): the
        # abandoned task is recorded here and warned about after the Iteration is
        # judged, and changes neither the outcome nor the final message.
        if not self._belongs_to_session(event.get("session_id")):
            # I6: the observation must belong to the established session before it
            # can reach the operator. A foreign, missing, or empty session id is a
            # crossed stream -- the same class #49 tightened for assistant and
            # licensing events -- so it cannot be attributed to this Iteration and
            # produces no abandonment warning.
            return
        patch = event.get("patch")
        if not isinstance(patch, dict) or patch.get("status") != "killed":
            return
        task_id = event.get("task_id")
        label = task_id if isinstance(task_id, str) and task_id else None
        if label not in self.killed_tasks:
            # A duplicate explicit killed report for a task already recorded names
            # no additional task (I6): follow the stream's evidence rather than
            # fabricate a second abandonment from a repeated event. Reports that
            # carry no id all coalesce to a single unnamed entry for the same
            # reason -- there is nothing to tell two of them apart.
            self.killed_tasks.append(label)

    @property
    def fallback_models(self) -> list[str]:
        models = self.assistant_models + self.model_usage
        return list(dict.fromkeys(model for model in models if model != self.expected_model))


def resume_argv(worktree: Path, model: str, session: str) -> list[str]:
    # The interactive Claude command a handed-off session resumes into, minus the
    # Launch chain wrap that ``launch.session_argv`` adds around it. The worktree is
    # part of the interface but Claude keys off the process cwd, not a --dir flag.
    return [
        "claude",
        "--resume",
        session,
        "--model",
        model,
        "--dangerously-skip-permissions",
        "--setting-sources",
        "project",
        "--strict-mcp-config",
        "--settings",
        CLAUDE_SETTINGS,
    ]


def execute_iteration(
    worktree: Path,
    run_dir: Path,
    prompt: str,
    model: str,
    env: dict[str, str],
    timeout: float,
    sandbox_profile: Path | None = None,
) -> tuple[str, str | None, str | None]:
    # Confine the Claude session under Ralph's generated profile via the shared
    # `session_argv` wrap (register D6/D13): `caffeinate -im sandbox-exec -f
    # <profile> claude …`, caffeinate outermost. This is the exact OpenCode code
    # path — one launcher, no Claude-specific fork — and Ralph's outer Seatbelt
    # profile is authoritative: Ralph never enables Claude Code's own Bash sandbox
    # (it runs with --dangerously-skip-permissions and CLAUDE_SETTINGS leaves the
    # inner sandbox off), so the two do not fight and the outer profile is sole.
    stdout_path = run_dir / "stdout.ndjson"
    stderr_path = run_dir / "stderr.log"
    args = session_argv(
        [
            "claude",
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            "--model",
            model,
            "--setting-sources",
            "project",
            "--strict-mcp-config",
            "--settings",
            CLAUDE_SETTINGS,
        ],
        sandbox_profile,
    )
    result = ClaudeEventResult(model)
    try:
        process = subprocess.Popen(
            args,
            cwd=worktree,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            start_new_session=True,
        )
    except OSError as error:
        raise RalphError(f"could not start Claude: {error.strerror}") from None

    controller = ProcessController(process, timeout)
    controller.start()
    try:
        return _consume_claude_iteration(
            process,
            controller,
            result,
            run_dir,
            prompt,
            stdout_path,
            stderr_path,
        )
    finally:
        if process.poll() is None or controller.group_alive():
            controller.stop_gracefully()
        controller.finish()


def report_killed_tasks(result: ClaudeEventResult) -> None:
    # I7/H9: an accepted same-session explicit killed-task report is delivered to
    # the operator on every terminal path -- an ordinary completion and a
    # contract-failure handoff alike -- before the caller prints its outcome, so a
    # halt never hides work the CLI said it abandoned (#50 finding 5). Attribution
    # already required the established session id (`_note_killed_task`, I6); this
    # only reports what was accepted. It is observational and changes neither the
    # Iteration's outcome nor its final message (H1). Still a not-yet-migrated
    # operator-facing write, registered as Run console migration debt (G13/G14; the
    # terminal-ownership test names it and #36 is told to adopt and word it).
    for task_id in result.killed_tasks:
        print(
            "ralph: warning: Claude Code killed a background task still running when "
            "the backend ended its turn, so its work was left unverified: "
            + killed_task_label(task_id),
            file=sys.stderr,
        )


def _consume_claude_iteration(
    process: subprocess.Popen[str],
    controller: ProcessController,
    result: ClaudeEventResult,
    run_dir: Path,
    prompt: str,
    stdout_path: Path,
    stderr_path: Path,
) -> tuple[str, str | None, str | None]:
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    stderr_stream = process.stderr
    stderr_invalid: list[bool] = []

    def drain_stderr() -> None:
        with stderr_path.open("w", encoding="utf-8") as retained:
            try:
                for chunk in stderr_stream:
                    retained.write(redact(chunk))
            except UnicodeDecodeError:
                stderr_invalid.append(True)
                retained.write("\n[ralph: backend stderr contained invalid UTF-8]\n")

    thread = threading.Thread(target=drain_stderr, daemon=True)
    thread.start()
    message = {
        "type": "user",
        "message": {
            "role": "user",
            "content": prompt + active_protocol() + BACKGROUND_TASK_DIRECTIVE,
        },
        "parent_tool_use_id": None,
    }
    try:
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.close()
    except BrokenPipeError:
        process.stdout.close()
        process.wait()
        thread.join()
        raise_if_controlled_stop(controller, "Claude", result.session_id)
        raise RalphError("Claude exited before accepting the prompt") from None
    with stdout_path.open("w", encoding="utf-8") as retained:
        stdout_lines = iter(process.stdout)
        while True:
            try:
                line = next(stdout_lines)
            except StopIteration:
                break
            except UnicodeDecodeError:
                controller.force_kill()
                thread.join()
                write_claude_session(run_dir, result)
                report_killed_tasks(result)
                # A stopped backend can truncate its final output mid-character;
                # blame Ralph's own timeout or interrupt before the contract.
                raise_if_controlled_stop(controller, "Claude", result.session_id)
                raise_backend_contract_failure(
                    result.session_id, "Claude emitted invalid UTF-8 output"
                )
            retained.write(redact(line))
            retained.flush()
            try:
                event = json.loads(line)
                result.accept(event)
            except (json.JSONDecodeError, RecursionError):
                # RecursionError comes from JSON nested past the interpreter
                # limit (or a pathologically deep question payload); treat it as
                # malformed output and fail closed rather than let it escape as a
                # traceback past every handler.
                controller.force_kill()
                thread.join()
                write_claude_session(run_dir, result)
                report_killed_tasks(result)
                raise_if_controlled_stop(controller, "Claude", result.session_id)
                if result.session_id:
                    raise HandoffError(
                        "Claude emitted malformed structured output",
                        result.session_id,
                        outcome="backend_contract_failure",
                    ) from None
                raise RalphError("Claude emitted malformed structured output") from None
            except RalphError as error:
                controller.stop_gracefully()
                thread.join()
                write_claude_session(run_dir, result)
                report_killed_tasks(result)
                if isinstance(error, HandoffError):
                    raise
                # An interrupted Claude session emits an error result event
                # before exiting; when Ralph itself stopped the backend that
                # event is an artifact of the stop, not a contract violation,
                # so report the timeout or interruption instead.
                raise_if_controlled_stop(controller, "Claude", result.session_id)
                if result.session_id:
                    raise HandoffError(
                        str(error),
                        result.session_id,
                        outcome="backend_contract_failure",
                    ) from None
                raise
            if result.question:
                controller.stop_gracefully()
                thread.join()
                write_claude_session(run_dir, result)
                report_killed_tasks(result)
                if not result.session_id:
                    raise RalphError("Claude attempted a question before session creation")
                raise HandoffError(
                    "Claude attempted a native question tool",
                    result.session_id,
                    result.question,
                )
    returncode = process.wait()
    thread.join()
    write_claude_session(run_dir, result)
    # Deliver any accepted same-session kill before the outcome is judged, so a
    # completion, a backend failure, a controlled stop, or a post-EOF contract
    # failure below all still name the abandoned task (I7).
    report_killed_tasks(result)
    raise_if_controlled_stop(controller, "Claude", result.session_id)
    if stderr_invalid:
        raise_backend_contract_failure(
            result.session_id, "Claude emitted invalid UTF-8 on stderr"
        )
    if returncode:
        if result.session_id:
            raise HandoffError(
                "Claude session failed; see retained stderr",
                result.session_id,
                outcome="backend_failure",
            )
        raise RalphError("Claude session failed; see retained stderr")
    if (
        result.session_id is None
        or not result.turns
        or not result.results
        or not result.turns[-1].parent_texts
    ):
        if result.session_id:
            raise HandoffError(
                "Claude output omitted required session metadata or final result",
                result.session_id,
                outcome="backend_contract_failure",
            )
        raise RalphError("Claude output omitted required session metadata or final result")
    if len(result.results) != len(result.turns):
        # Each turn must produce exactly one result, attributed positionally; a
        # count that does not match means the stream ended mid-turn or in a shape
        # nobody has studied, so fail closed rather than judge a partial session.
        raise_backend_contract_failure(
            result.session_id, "Claude produced a result for only some of its turns"
        )
    final = result.final_text
    # The guards above (session id, a result per turn, a Backend message backing
    # the final turn) establish that the final result is present, so `final` is
    # never None here; assert it so the marker scans below type-check.
    assert final is not None
    explicit = explicit_needs_input(final)
    if explicit:
        # The final parent turn is authoritative for a needs-input halt.
        raise HandoffError("Claude requested operator input", result.session_id, explicit)
    # A NEEDS_INPUT marker the Backend raised in any message other than the one
    # the Iteration is judged on is not a halt: the Backend itself took the
    # question back (Probe B showed it re-emitting or dropping the marker per
    # turn), so warn and continue rather than cost the Iteration -- mirroring the
    # explicit-versus-inferred split. The withdrawal is per *message*: a marker
    # spoken past within the final turn is the same withdrawal as one spoken past
    # across turns, and neither may vanish silently.
    withdrawn: str | None = None
    for superseded in result.superseded_texts:
        withdrawn = explicit_needs_input(superseded)
        if withdrawn:
            break
    if withdrawn:
        # A bounded interruption, not an outlet for the whole final message: the
        # fragment is redacted, then collapsed and capped for display (issue #39).
        print(
            "ralph: warning: the backend requested operator input earlier in the "
            "session but its final message withdrew it; continuing to the next "
            f"iteration: {bounded_quote(redact(withdrawn))}",
            file=sys.stderr,
        )
    inferred = inferred_needs_input(final)
    if inferred:
        # An unmarked concluding question is a low-confidence signal; the loop must
        # not take the irreversible operator-halt on a guess. Surface it and let the
        # next iteration re-derive from the tracker.
        print(
            "ralph: warning: final message ended on an unmarked operator-directed "
            "question; continuing to the next iteration (no <promise>NEEDS_INPUT</promise> "
            f"marker and no question tool used): {bounded_quote(redact(inferred))}",
            file=sys.stderr,
        )
    complete = has_completion_marker(final)
    # The concluding message rides back with the outcome so the Run console can show
    # it in the Iteration's outcome block; it is the same final message the marker was
    # read from. The Loop truncates it for display only (register G14).
    return ("complete" if complete else "budget_exhausted"), result.session_id, final


def write_claude_session(run_dir: Path, result: ClaudeEventResult) -> None:
    write_json(
        run_dir / "session.json",
        {
            "assistant_models": result.assistant_models,
            "fallback_models": result.fallback_models,
            "final_result_received": result.final_text is not None,
            "initial_model": result.initial_model,
            "model_usage": result.model_usage,
            "session_id": result.session_id,
        },
    )
