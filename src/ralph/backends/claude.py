"""The Claude Backend adapter: preflight, customization refusal, Claude constants
and host paths, event accumulation, iteration, and session persistence.

Invariants:
- The init event is the single proof of a subscription-safe session: it must report
  ``apiKeySource == "none"`` (billing rides the proven pro/max OAuth login, not a
  metered key), ``bypassPermissions`` full-auto mode, no external MCP servers or
  plugins, and a tool set that is a subset of ``CLAUDE_BUILTIN_TOOLS`` — anything
  else fails closed. The session id is checkpointed before the rest of the init is
  validated so a later contract failure is still a resumable handoff.
- The terminal ``result`` must be the final event: any event after it violates the
  ordered contract and fails closed, and the result text must agree with the
  assembled final assistant response so a contradictory result is never trusted.
- ``--unsafe-allow-agents`` relaxes only the agent vectors (``.claude/agents`` and
  the settings ``agent`` key). Managed, server-managed, hooks, plugins, and every
  other unsafe settings key stay refused and are checked *before* the local
  customization gather, so the opt-out hint is advertised only when an agent vector
  is the sole blocker and never masquerades as a remedy for something it cannot fix.
- A stop Ralph itself caused (timeout/interrupt) is classified *before* any contract
  failure, so an interrupted session's error result is never misread as misbehavior.

Depends on / must not know: ``errors``, ``launch`` (caffeinate), ``process``,
``protocol``, ``redaction`` (functions only), ``gitcontext``, and ``preflight``. It
must not know how the Loop schedules Iterations; the Loop must not know these
helpers exist beyond the five Backend interface names.

See also: ``backends`` (dispatch/registry), ``backends.opencode`` (twin adapter),
``launch`` (the wrapped argv), ``protocol`` (marker detection).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any

from ..errors import HandoffError, RalphError, raise_backend_contract_failure
from ..gitcontext import command, write_json
from ..launch import caffeinate_executable
from ..preflight import common_preflight, version_tuple
from ..process import ProcessController, raise_if_controlled_stop
from ..protocol import (
    PROTOCOL,
    extract_question,
    has_completion_marker,
    needs_input_question,
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
# in its init event (observed against 2.1.211). MCP tools are namespaced
# `mcp__server__tool` and plugin tools carry their own names, so anything
# outside this set still fails the subset assertion closed.
CLAUDE_BUILTIN_TOOLS = {
    "Agent",
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


def claude_preflight(
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


class ClaudeEventResult:
    def __init__(self, model: str) -> None:
        self.expected_model = model
        self.session_id: str | None = None
        self.initial_model: str | None = None
        self.assistant_models: list[str] = []
        self.assistant_results: list[str] = []
        self.final_text: str | None = None
        self.model_usage: list[str] = []
        self.question: str | None = None

    def accept(self, event: Any) -> None:
        if self.final_text is not None:
            # The terminal result must be the final event in the stream. A
            # duplicate result, a late assistant message, trailing init, or any
            # other event after it means the ordered contract was violated, so
            # fail closed rather than assess a stream we no longer trust.
            raise RalphError("Claude emitted an event after the terminal result")
        if not isinstance(event, dict):
            return
        event_type = event.get("type")
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
            if self.session_id is not None and self.session_id != session_id:
                raise RalphError("Claude stream contained inconsistent session metadata")
            self.session_id = session_id
        model = event.get("model")
        if not isinstance(session_id, str) or not session_id or not isinstance(model, str):
            raise RalphError("Claude initialization omitted required metadata")
        if self.initial_model is not None:
            raise RalphError("Claude emitted duplicate initialization metadata")
        self.initial_model = model
        if model != self.expected_model:
            raise RalphError("Claude initial model did not match the selected model")
        # `apiKeySource` reports where a metered API key came from. A real
        # subscription-OAuth session (Claude Code >= 2.1.208) reports "none":
        # no API key is in play, so billing rides the OAuth login that
        # preflight already proved is a pro/max subscription. Any other value
        # ("ANTHROPIC_API_KEY", "apiKeyHelper", ...) means a metered key was
        # loaded, so fail closed.
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

    def _accept_assistant(self, event: dict[str, Any]) -> None:
        self._require_session(event.get("session_id"))
        message = event.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("model"), str):
            raise RalphError("Claude assistant event omitted required metadata")
        model = message["model"]
        if not model.startswith("claude-"):
            raise RalphError("Claude used a non-subscription model fallback")
        self.assistant_models.append(model)
        content = message.get("content")
        if not isinstance(content, list):
            raise RalphError("Claude assistant event omitted content")
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
                    self.question = extract_question(part.get("input")) or "Claude attempted to ask a question."
                print(redact(f"[{name if isinstance(name, str) and name else 'tool'}]"), flush=True)
        text = "".join(texts)
        if text:
            self.assistant_results.append(text)

    def _accept_result(self, event: dict[str, Any]) -> None:
        self._require_session(event.get("session_id"))
        if event.get("subtype") != "success" or event.get("is_error") is not False:
            raise RalphError("Claude session reported an unsuccessful result")
        result = event.get("result")
        if not isinstance(result, str) or not result:
            raise RalphError("Claude result omitted the final assistant response")
        # The terminal result text must agree with the assembled final assistant
        # response so a contradictory result (a different final answer than the
        # one that was streamed) fails closed rather than being trusted.
        if self.assistant_results and result.strip() != self.assistant_results[-1].strip():
            raise RalphError("Claude terminal result disagreed with the final assistant response")
        usage = event.get("modelUsage")
        if not isinstance(usage, dict) or any(
            not isinstance(model, str) or not model.startswith("claude-") for model in usage
        ):
            raise RalphError("Claude result omitted valid model usage")
        self.model_usage = list(usage)
        self.final_text = result

    def _require_session(self, value: Any) -> None:
        if self.session_id is None or value != self.session_id:
            raise RalphError("Claude stream contained inconsistent session metadata")

    @property
    def fallback_models(self) -> list[str]:
        models = self.assistant_models + self.model_usage
        return list(dict.fromkeys(model for model in models if model != self.expected_model))


def execute_claude_iteration(
    worktree: Path,
    run_dir: Path,
    prompt: str,
    model: str,
    env: dict[str, str],
    timeout: float,
) -> tuple[str, str | None]:
    stdout_path = run_dir / "stdout.ndjson"
    stderr_path = run_dir / "stderr.log"
    args = [
        caffeinate_executable(),
        "-im",
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
    ]
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
        "message": {"role": "user", "content": prompt + PROTOCOL},
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
        or result.initial_model is None
        or not result.assistant_results
        or result.final_text is None
    ):
        if result.session_id:
            raise HandoffError(
                "Claude output omitted required session metadata or final result",
                result.session_id,
                outcome="backend_contract_failure",
            )
        raise RalphError("Claude output omitted required session metadata or final result")
    question = needs_input_question(result.final_text)
    if question:
        raise HandoffError("Claude requested operator input", result.session_id, question)
    complete = has_completion_marker(result.final_text)
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
