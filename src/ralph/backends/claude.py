"""The Claude Backend adapter: preflight, customization refusal, Claude constants
and host paths, event accumulation, iteration, and session persistence.

Invariants:
- Each ``system/init`` opens a turn and re-proves the subscription-safe session: it
  must report ``apiKeySource == "none"`` (billing rides the proven pro/max OAuth
  login, not a metered key), ``bypassPermissions`` full-auto mode, no external MCP
  servers or plugins, a tool set that is a subset of ``CLAUDE_BUILTIN_TOOLS``, and
  the same session id — every turn, so a longer stream is a stronger proof, not a
  weaker one. Anything else fails closed. The session id is checkpointed on the
  first init before the rest is validated so a later contract failure is still a
  resumable handoff.
- The stream is read to EOF and may carry multiple turns. A second (or later) init
  is admitted only in a session that registered a background task
  (``background_tasks_changed``); an unexplained duplicate init stays the hard
  ``Claude emitted duplicate initialization metadata`` failure it is today. Results
  are flushed at EOF in turn order — a ``result`` does *not* close a turn. The i-th
  result belongs to the i-th turn; Ralph asserts equal counts and that each result
  agrees with its turn's last message from the Backend itself, failing closed
  otherwise. The Iteration's outcome is judged on the final turn.
- ``parent_tool_use_id`` distinguishes the Backend's own messages (``None``) from a
  background subagent's (the launching tool-use id). Only the Backend's own messages
  assemble a turn's response and are scanned for completion/needs-input markers, so a
  subagent speaking last is never mistaken for the answer; the subagent's messages
  still stay in the retained stream evidence. The native question tool halts wherever
  it appears, unfiltered by origin.
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
``process``, ``protocol``, ``redaction`` (functions only), ``gitcontext``, and
``preflight``. It must not know how the Loop schedules Iterations; the Loop must not
know these helpers exist beyond the five Backend interface names.

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


def reject_claude_customizations(worktree: Path, allow_agents: bool = False) -> None:
    claude_dir = worktree / ".claude"
    # --unsafe-allow-agents relaxes only the agent vectors: the
    # `.claude/agents` directory and the settings.json `agent` key. It exists for
    # repos whose loop develops or depends on subagents. Hooks, plugins, managed
    # configuration, and every other unsafe setting stay refused, and runtime
    # MCP/plugin/tool isolation is still proven from the init event. The trade is
    # deliberate and unsafe: Ralph can no longer prove which subagents loaded, so
    # the operator vouches for them for this run.
    if allow_agents and (claude_dir / "agents").exists():
        print(
            "WARNING: --unsafe-allow-agents is set; Ralph is not proving "
            "Claude subagent isolation for this run.",
            file=sys.stderr,
        )
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
        return
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
) -> None:
    common_preflight(worktree, slug, "claude", env)
    reject_claude_customizations(worktree, allow_agents)
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


def synthetic_error_reason(event: dict[str, Any], message: dict[str, Any]) -> str:
    # Turn a Claude Code synthetic error message into an operator-facing halt
    # reason. The human-readable error text lives in the message content; the
    # machine-readable cause lives in the event's top-level `error` field.
    detail = " ".join(
        part["text"]
        for part in (message.get("content") if isinstance(message.get("content"), list) else [])
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
        self.assistant_models: list[str] = []
        self.results: list[str] = []
        self.model_usage: list[str] = []
        self.question: str | None = None

    @property
    def initial_model(self) -> str | None:
        return self.turns[0].initial_model if self.turns else None

    @property
    def final_text(self) -> str | None:
        # The Iteration's answer is the final turn's result; results are flushed
        # at EOF in turn order, so the last one is the final turn's.
        return self.results[-1] if self.results else None

    def accept(self, event: Any) -> None:
        if self.results:
            # Results are flushed at EOF in turn order. Once every turn's result
            # has arrived nothing more may follow, and while they are still
            # arriving only further results may -- a late init, assistant, or any
            # other event means the ordered contract was violated. Fail closed
            # rather than assess a stream we no longer trust. (In the single-turn
            # case the very first result completes the count, so a second result
            # or a trailing message is caught here exactly as before.)
            results_complete = len(self.results) >= len(self.turns)
            is_result = isinstance(event, dict) and event.get("type") == "result"
            if results_complete or not is_result:
                raise RalphError("Claude emitted an event after the terminal result")
        if not isinstance(event, dict):
            return
        event_type = event.get("type")
        if event_type == "system" and event.get("subtype") == "background_tasks_changed":
            # A subagent launched with run_in_background finishes asynchronously,
            # after the turn that launched it, and forces the CLI to open a second
            # turn with a fresh `system` init. That registration is what licenses
            # the later init: a duplicate init is admitted only in a session that
            # recorded a background task here, and an unexplained one still fails
            # closed. Synchronous subagents return inline as a tool result and
            # never emit this, so they never flip the gate.
            self.background_seen = True
            return
        if event_type == "system" and event.get("subtype") == "init":
            self._accept_init(event)
            return
        if event_type == "assistant":
            self._accept_assistant(event)
            return
        if event_type == "result":
            self._accept_result(event)

    def _accept_init(self, event: dict[str, Any]) -> None:
        session_id = event.get("session_id")
        if isinstance(session_id, str) and session_id:
            # Checkpoint the session id before validating the rest of the event so
            # a later contract failure in a partially malformed init is still a
            # consuming, resumable handoff rather than an unrecoverable failure.
            # Every turn must carry the same id.
            if self.session_id is not None and self.session_id != session_id:
                raise RalphError("Claude stream contained inconsistent session metadata")
            self.session_id = session_id
        model = event.get("model")
        if not isinstance(session_id, str) or not session_id or not isinstance(model, str):
            raise RalphError("Claude initialization omitted required metadata")
        if self.turns and not self.background_seen:
            # A second (or later) init opens a new turn only in a session that
            # registered a background task; otherwise it is the unexplained
            # duplicate init that has always failed closed.
            raise RalphError("Claude emitted duplicate initialization metadata")
        if model != self.expected_model:
            raise RalphError("Claude initial model did not match the selected model")
        # Every init re-proves the full Trust boundary, so a longer stream is a
        # stronger proof, not a weaker one. `apiKeySource` "none" means no metered
        # API key is in play, so billing rides the OAuth login preflight proved is
        # a pro/max subscription; `bypassPermissions` is full-auto mode; the MCP,
        # plugin, and tool sets prove no external customization loaded. Any other
        # value fails closed -- on the first turn and on every turn after it.
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
        self.turns.append(ClaudeTurn(model))

    def _accept_assistant(self, event: dict[str, Any]) -> None:
        self._require_session(event.get("session_id"))
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
        # `parent_tool_use_id` is None for the Backend's own messages and the
        # launching tool-use id for a background subagent's. Only the Backend's
        # own text assembles the turn's response and is later scanned for markers,
        # so a subagent speaking last is never mistaken for the answer; the
        # subagent's output is still printed and retained as evidence.
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

    def _require_session(self, value: Any) -> None:
        if self.session_id is None or value != self.session_id:
            raise RalphError("Claude stream contained inconsistent session metadata")

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
) -> tuple[str, str | None]:
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


def _consume_claude_iteration(
    process: subprocess.Popen[str],
    controller: ProcessController,
    result: ClaudeEventResult,
    run_dir: Path,
    prompt: str,
    stdout_path: Path,
    stderr_path: Path,
) -> tuple[str, str | None]:
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    stderr_invalid: list[bool] = []

    def drain_stderr() -> None:
        with stderr_path.open("w", encoding="utf-8") as retained:
            try:
                for chunk in process.stderr:
                    retained.write(redact(chunk))
            except UnicodeDecodeError:
                stderr_invalid.append(True)
                retained.write("\n[ralph: backend stderr contained invalid UTF-8]\n")

    thread = threading.Thread(target=drain_stderr, daemon=True)
    thread.start()
    message = {
        "type": "user",
        "message": {"role": "user", "content": prompt + active_protocol()},
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
    explicit = explicit_needs_input(final)
    if explicit:
        # The final parent turn is authoritative for a needs-input halt.
        raise HandoffError("Claude requested operator input", result.session_id, explicit)
    # A NEEDS_INPUT marker an earlier turn raised but the final turn withdrew is
    # not a halt: the Backend itself took the question back (Probe B showed it
    # re-emitting or dropping the marker per turn), so warn and continue rather
    # than cost the Iteration -- mirroring the explicit-versus-inferred split.
    withdrawn: str | None = None
    for earlier in result.results[:-1]:
        withdrawn = explicit_needs_input(earlier)
        if withdrawn:
            break
    if withdrawn:
        print(
            "ralph: warning: an earlier turn requested operator input but the final "
            "turn withdrew it; continuing to the next iteration:\n"
            f"{redact(withdrawn)}",
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
            f"marker and no question tool used):\n{redact(inferred)}",
            file=sys.stderr,
        )
    complete = has_completion_marker(final)
    return ("complete" if complete else "budget_exhausted"), result.session_id


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
