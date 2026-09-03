"""The OpenCode Backend adapter: preflight, agent refusal, isolated config, event
accumulation, iteration (including second-pass session verification), and session
persistence.

Invariants:
- The effective configuration is the single authoritative proof of isolation:
  ``--pure`` still loads project/global agents into the ``agent`` map, so a
  non-empty map fails closed unless ``--unsafe-allow-agents`` admits it; provider
  routing, model availability, and the sanitized ``isolated_config`` are all
  re-proven from ``debug config`` before budget is spent. When the flag admits a
  non-empty map, ``preflight`` returns a ``console.Deviation`` (else ``None``) so the
  caller states the relaxed agent-isolation guarantee loudly through the Run console;
  the adapter words no operator-facing warning of its own (register G7/G14).
- Progress is emitted as Observations through the injected sink during the stream
  (register G4/G5, #41), so the status line means the same on an OpenCode run as on a
  Claude one without this adapter writing to a terminal. Each tool call emits its tool
  use (``ToolObserved``) once, deduplicated by part id so a tool part's several state
  updates count as one; each step-finish part emits the orchestrator's live context
  gauge (``ContextObserved``) -- the input plus cache-read tokens it reports, this
  Backend's arithmetic for the shared vocabulary, absent when no usable token metadata
  is present. OpenCode emits no subagent roster and no task progress, so this adapter
  sends no ``SubagentsObserved`` at all and the Run console renders the subagent count
  as absent rather than a fabricated zero (register G5). The Backend's running
  commentary rides the same sink -- its streamed message text as a partial
  ``console.Narrated``, each tool-use state update as a ``console.ToolActivity``, and
  its step boundaries as ``console.StepObserved`` -- so this adapter no longer writes
  to a terminal at all (register G13). Whether any of it is shown is the Run console's
  decision: suppressed by default, restored only under the opt-in feed (register
  G2/G11). The text is reported as *partial* because OpenCode streams deltas rather
  than whole messages, so the console holds the speaker's line open until the text
  completes it; the incremental redaction below still happens here, because only this
  side knows what was already reported. The Stage rides the same sink
  (``console.StageObserved``, register G6, #45), read by ``protocol.extract_stage``
  from that same streaming text -- every update of a growing text part, and every
  delta that could close a marker, so whichever shape carries a declaration reports
  it -- rather than only from the final message, and never inferred from the tool
  use around it. A declaration is announced once per part: a growing part restates it on
  every update, and re-announcing would keep restarting the staleness clock the
  console falls back to the last tool on.
- The unmarked-question warning is emitted as an ``UnmarkedQuestion`` Observation (the
  raw fragment), and the Run console redacts, bounds, and words it into a bounded
  interruption, never the Backend's whole final message (#39/#41). This adapter
  constructs no operator-facing text for it; the load-bearing ``unmarked
  operator-directed`` phrase lives in the console now (register G14/G17/G19).
- Live text is diffed redacted-against-redacted: the whole accumulated part is
  redacted, then compared to what was already shown, so a secret that only
  completes across streaming chunk boundaries can never leak to the console even
  though the retained log is redacted a line at a time.
- A single ``sessionID`` must hold across the stream; inconsistent session metadata
  or an alternate/malformed provider route fails closed. After the run, a
  second-pass ``export`` re-verifies the persisted session's routing and final text
  independently — this verification is internal to the adapter and invisible to the
  Loop (register E2). That export is captured to an unlinked temporary file,
  never a pipe: OpenCode writes the whole session at once and exits without
  waiting for the write to land, so a session past the pipe buffer would be read
  truncated under a zero exit status and a delivered iteration would be rejected
  as a contract failure. Unlinked because the capture is raw backend output: no
  crash may leave it behind as the one retained artifact that never went through
  ``redact``.
- A stop Ralph itself caused (timeout/interrupt) is classified *before* any
  contract failure, so a truncated or error-closed stream is never misreported as
  backend misbehavior.
- ``execute_iteration`` returns the verified final message alongside the outcome and
  session id, so the Run console can show it in the Iteration's outcome block; it is
  the same text the completion/needs-input markers were read from, returned raw, and
  the console truncates it for display only (register G14). The outcome names what
  happened to *this Iteration* -- ``complete`` or ``incomplete`` -- and never the
  run-level word for a spent budget (register J3). It receives the injected
  ``ObservationSink`` and drives it through the accumulator (the progress facts above).

Depends on / must not know: ``environment`` (the sanitized base and the timeout
ceiling its ``environment`` layers on), ``errors``, ``launch`` (``session_argv``),
``process``, ``protocol``, ``redaction`` (functions only), ``gitcontext``,
``preflight``, and ``console`` (the ``Deviation`` value type ``preflight`` returns
and the frozen Observation value types it emits through the injected
``ObservationSink`` — never a console instance, and it words none of them). It must
not know how the Loop schedules Iterations, nor how the Run console renders an
Observation; the Loop must not know these helpers exist beyond the five Backend
interface names.

See also: ``backends`` (registry and the five-name Protocol), ``backends.claude``
(twin adapter), ``launch`` (``session_argv``, the wrapped argv), ``protocol``
(marker detection and ``active_protocol``, the run's protocol text appended here).
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from typing import Any, TYPE_CHECKING

from ..console import (
    OPENCODE_AGENTS_DEVIATION,
    ContextObserved,
    Deviation,
    Narrated,
    Observation,
    StageObserved,
    StepObserved,
    ToolActivity,
    ToolObserved,
    UnmarkedQuestion,
)
from ..environment import BACKEND_TIMEOUT_MS, clean_environment
from ..errors import (
    HandoffError,
    RalphError,
    StartedIterationError,
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
    extract_stage,
    has_completion_marker,
    inferred_needs_input,
)
from ..redaction import redact

if TYPE_CHECKING:
    from . import ObservationSink


MIN_OPENCODE_VERSION = (1, 17, 20)


def validate_model(model: str) -> None:
    if not model.startswith("openai/") or model == "openai/":
        raise RalphError("model must use the openai/ provider")


def environment(model: str) -> dict[str, str]:
    # The sanitized base plus OpenCode's routing keys: the isolated configuration
    # pinned as inline content so no on-disk config can reroute the session,
    # external-plugin and autoupdate suppression, and the Bash-tool timeout pinned
    # to the 32-bit ceiling so Ralph's own iteration timer stays authoritative.
    env = clean_environment()
    # OpenAI subscription OAuth is implemented by OpenCode's built-in Codex auth
    # plugin, so an ambient opt-out must not survive into the isolated session.
    env.pop("OPENCODE_DISABLE_DEFAULT_PLUGINS", None)
    env.update(
        {
            "OPENCODE_CONFIG_CONTENT": json.dumps(isolated_config(model), separators=(",", ":")),
            "OPENCODE_DISABLE_AUTOUPDATE": "true",
            "OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS": str(BACKEND_TIMEOUT_MS),
        }
    )
    return env


def isolated_config(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "small_model": model,
        "enabled_providers": ["openai"],
        "provider": {"openai": {"options": {"timeout": False}}},
        "mcp": {},
        "plugin": [],
        "share": "disabled",
        "autoupdate": False,
        "formatter": False,
        "lsp": False,
    }


def strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)


def validate_opencode_auth_output(value: str) -> None:
    lines = [line.strip() for line in strip_ansi(value).splitlines() if line.strip()]
    error = "OpenCode must have exactly one understood OpenAI OAuth credential; output is unfamiliar or ambiguous"
    if len(lines) != 5:
        raise RalphError(error)
    if not re.fullmatch(r"┌\s+Credentials\s+.+", lines[0]) or lines[1] != "│" or lines[3] != "│":
        raise RalphError(error)
    if not re.fullmatch(r"[●•]\s+OpenAI\s+oauth", lines[2]):
        raise RalphError(error)
    if not re.fullmatch(r"└\s+1\s+credential(?:s)?", lines[4]):
        raise RalphError(error)


def validate_effective_config(config: Any, model: str) -> None:
    if not isinstance(config, dict):
        raise RalphError("effective OpenCode configuration is not an object")
    expected = isolated_config(model)
    for key in ("model", "small_model", "enabled_providers", "mcp", "plugin", "share", "autoupdate", "formatter", "lsp"):
        if config.get(key) != expected[key]:
            raise RalphError("effective OpenCode configuration is not subscription-safe")
    provider = config.get("provider")
    if provider != expected["provider"]:
        raise RalphError("effective OpenCode configuration has ambiguous provider routing")


def reject_custom_tools(worktree: Path) -> None:
    roots = [worktree / ".opencode", Path.home() / ".config" / "opencode"]
    for root in roots:
        if any((root / name).exists() for name in ("tool", "tools", "plugin", "plugins")):
            raise RalphError("external plugins or custom tools must be disabled before running Ralph")


# OpenCode counterpart to AGENT_OPT_OUT_HINT. The agent check runs after every
# other OpenCode preflight proof, so when this refusal fires the agent map is
# by construction the only blocker and the opt-out is never a false remedy.
OPENCODE_AGENT_REFUSAL = (
    "OpenCode agents must be disabled before running Ralph; you may re-run with "
    "--unsafe-allow-agents to admit the effective configuration's agents for "
    "this run (unsafe: Ralph then cannot prove OpenCode agent isolation)"
)


def reject_opencode_agents(config: dict[str, Any], allow_agents: bool) -> Deviation | None:
    # OpenCode loads project (`.opencode/agent`) and global agent definitions
    # even under `--pure`, and they all surface in the effective configuration's
    # `agent` map, so that map is the single authoritative proof of agent
    # isolation. An unfamiliar shape fails closed like every other preflight
    # proof. --unsafe-allow-agents admits a non-empty map with the same trade as
    # the Claude backend: the operator vouches for the agents for this run. That
    # relaxed guarantee is stated loudly by the Run console (register G7/G14), so
    # this returns the deviation for the caller to word rather than printing it.
    agents = config.get("agent")
    if not isinstance(agents, dict):
        raise RalphError("effective OpenCode configuration omitted the agent map")
    if not agents:
        return None
    if not allow_agents:
        raise RalphError(OPENCODE_AGENT_REFUSAL)
    return Deviation(OPENCODE_AGENTS_DEVIATION)


def preflight(
    worktree: Path, slug: str, model: str, env: dict[str, str], allow_agents: bool = False
) -> Deviation | None:
    common_preflight(worktree, slug, "opencode", env)
    reject_custom_tools(worktree)

    version = command(["opencode", "--version"], cwd=worktree, env=env).stdout
    if version_tuple(version) < MIN_OPENCODE_VERSION:
        raise RalphError("OpenCode 1.17.20 or newer is required")
    auth = command(["opencode", "--pure", "auth", "list"], cwd=worktree, env=env).stdout
    validate_opencode_auth_output(auth)

    resolved = command(["opencode", "--pure", "debug", "config"], cwd=worktree, env=env).stdout
    try:
        config = json.loads(resolved)
    except json.JSONDecodeError:
        raise RalphError("effective OpenCode configuration is malformed") from None
    validate_effective_config(config, model)
    models = command(["opencode", "--pure", "models", "openai"], cwd=worktree, env=env).stdout.splitlines()
    if model not in {item.strip() for item in models}:
        raise RalphError(f"selected model is unavailable: {model}")
    # Checked after every other proof so the opt-out hint in the refusal is
    # advertised only when the agent map truly is the sole remaining blocker. Its
    # admitted-agents deviation, if any, rides back for the caller to state loudly
    # through the Run console (register G7/G14).
    return reject_opencode_agents(config, allow_agents)


def orchestrator_context(tokens: Any) -> int | None:
    # The orchestrator's live context size for the status gauge (register G5): the
    # input plus cache-read tokens OpenCode reports on a step-finish part. This is
    # OpenCode's arithmetic; the Claude adapter owns a different sum. OpenCode's own
    # reported total already folds cache reads in, so it is the wrong figure -- the
    # gauge wants the size of the prompt the orchestrator will resend, which is the
    # fresh input plus the cache it reads back. Returns None when no usable token
    # metadata is present (nothing to show), so a stream without it simply leaves the
    # gauge blank rather than asserting a zero.
    if not isinstance(tokens, dict):
        return None
    total = 0
    seen = False
    value = tokens.get("input")
    if isinstance(value, int) and not isinstance(value, bool):
        total += value
        seen = True
    cache = tokens.get("cache")
    if isinstance(cache, dict):
        read = cache.get("read")
        if isinstance(read, int) and not isinstance(read, bool):
            total += read
            seen = True
    return total if seen else None


class EventResult:
    def __init__(self, model: str, observe: "ObservationSink | None" = None) -> None:
        self.expected_model = model
        self.session_id: str | None = None
        self.assistant_messages: list[str] = []
        self.assistant_models: list[str] = []
        self.parts: dict[str, tuple[str, str]] = {}
        self.reported: dict[str, str] = {}
        self.question: str | None = None
        self.backend_error: str | None = None
        # Part ids of tool calls already counted, so a tool part that streams several
        # state updates (pending -> running -> completed) under one id is reported to
        # the status line once, not once per update (register G4).
        self.tool_calls_seen: set[str] = set()
        # The Stage label last announced for each text part id (register G6).
        self.stage_by_part: dict[str, str] = {}
        # The narrow Observation sink the Loop injects (the Run console). Progress
        # facts and the migrated warning are emitted through it so this adapter
        # constructs no operator-facing text of its own (register G14). ``None`` is a
        # no-op, so the accumulator still runs when no sink is injected.
        self._observe = observe

    def emit(self, observation: Observation) -> None:
        if self._observe is not None:
            self._observe.observe(observation)

    def accept(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        if isinstance(event.get("sessionID"), str):
            self._session(event["sessionID"])
        if event.get("type") == "error":
            error = event.get("error")
            if isinstance(error, dict):
                name = error.get("name")
                data = error.get("data")
                message = data.get("message") if isinstance(data, dict) else None
                if isinstance(name, str) and isinstance(message, str):
                    self.backend_error = redact(f"OpenCode {name}: {message}")
                    return
            self.backend_error = "OpenCode reported a backend error"
            return
        direct_part = event.get("part")
        if event.get("type") == "text" and isinstance(direct_part, dict):
            self._accept_text_part(direct_part, trusted=True)
            return
        if event.get("type") in {"tool_use", "step_start", "step_finish"} and isinstance(direct_part, dict):
            self._report_progress(event["type"], direct_part)
            if event.get("type") == "tool_use":
                self._accept_tool(direct_part)
            elif event.get("type") == "step_finish":
                self._accept_step_finish(direct_part)
            return
        props = event.get("properties")
        if not isinstance(props, dict):
            return
        info = props.get("info")
        if event.get("type") == "message.updated" and isinstance(info, dict) and info.get("role") == "assistant":
            message_id = info.get("id")
            message_session = info.get("sessionID")
            provider = info.get("providerID")
            model = info.get("modelID")
            if (
                not isinstance(message_id, str)
                or not isinstance(message_session, str)
                or not isinstance(provider, str)
                or not isinstance(model, str)
            ):
                raise RalphError("OpenCode assistant event omitted routing metadata")
            self._session(message_session)
            self._accept_assistant_route(provider, model)
            if message_id not in self.assistant_messages:
                self.assistant_messages.append(message_id)
            return
        part = props.get("part")
        if event.get("type") == "message.part.updated" and isinstance(part, dict):
            self._session(part.get("sessionID"))
            if part.get("type") == "tool":
                self._accept_tool(part)
                return
            if part.get("type") == "step-finish":
                self._accept_step_finish(part)
                return
            if part.get("type") != "text" or not isinstance(part.get("text"), str):
                return
            self._accept_text_part(part, trusted=False)
            return
        if event.get("type") == "message.part.delta":
            self._session(props.get("sessionID"))
            part_id = props.get("partID")
            delta = props.get("delta")
            if isinstance(part_id, str) and isinstance(delta, str) and part_id in self.parts:
                message_id, text = self.parts[part_id]
                self.parts[part_id] = (message_id, text + delta)
                if ">" in delta and message_id in self.assistant_messages:
                    # A delta extends the Backend's text without restating it, so a
                    # declaration it completes is visible on this shape alone. Only a
                    # delta carrying a ``>`` is scanned: a declaration ends on one, so
                    # every completed marker lands in such a delta, and the ordinary
                    # prose delta no longer re-parses the whole accumulated part.
                    self._report_stage(part_id, text + delta)

    def _accept_text_part(self, part: dict[str, Any], *, trusted: bool) -> None:
        message_id = part.get("messageID")
        part_id = part.get("id")
        text = part.get("text")
        if not isinstance(message_id, str) or not isinstance(part_id, str) or not isinstance(text, str):
            return
        if trusted and message_id not in self.assistant_messages:
            self.assistant_messages.append(message_id)
        self.parts[part_id] = (message_id, text)
        if trusted or message_id in self.assistant_messages:
            # Redact the whole accumulated part text, then diff against what was
            # already shown. Diffing the raw suffix instead would split a secret
            # across streaming chunk boundaries so neither fragment matched the
            # full value -- leaking it to the live console even though the
            # retained log (redacted a whole line at a time) stayed safe.
            # Comparing redacted-to-redacted closes that gap.
            redacted = redact(text)
            shown = self.reported.get(part_id, "")
            if redacted.startswith(shown):
                addition = redacted[len(shown) :]
            else:
                # A secret only completed once this chunk arrived, so the
                # already-reported prefix changed under redaction. Report the fully
                # redacted part on a fresh line rather than emit a raw fragment.
                addition = ("\n" if shown else "") + redacted
            if addition:
                # A fragment of a message still streaming, not a whole one: the Run
                # console holds the speaker's line open until the text completes it,
                # so a prefix never lands mid-sentence (register G11). The adapter
                # emits the fact; the console decides whether the opt-in feed shows
                # it and words the prefix (register G2/G14).
                self.emit(Narrated(addition, partial=True))
            self.reported[part_id] = redacted
            self._report_stage(part_id, text)

    def _report_stage(self, part_id: str, text: str) -> None:
        # The raw label is what is parsed: ``extract_stage`` bounds and sanitizes it,
        # and the Run console redacts it at its single choke point (register G17).
        # A declaration in a later part is a fresh announcement even when it names the
        # stage already showing -- the Backend re-entering one, as the protocol invites
        # -- so the clock is kept per part rather than globally.
        stage = extract_stage(text)
        if stage is None or self.stage_by_part.get(part_id) == stage:
            return
        self.stage_by_part[part_id] = stage
        self.emit(StageObserved(stage))

    def _report_progress(self, event_type: str, part: dict[str, Any]) -> None:
        # The feed's progress markers, reported as facts the Run console words: a
        # tool-use update with whatever state the stream named, or one of OpenCode's
        # own step boundaries. Every state update is reported, unlike the status
        # line's one-per-call ``ToolObserved`` (register G4/G14).
        if event_type == "tool_use":
            name = part.get("tool")
            tool = name if isinstance(name, str) else "tool"
            state = part.get("state")
            status = state.get("status") if isinstance(state, dict) else None
            self.emit(ToolActivity(tool, state=status if isinstance(status, str) else None))
            return
        self.emit(StepObserved(started=event_type == "step_start"))

    def _accept_tool(self, part: dict[str, Any]) -> None:
        tool = part.get("tool")
        part_id = part.get("id")
        # Report the orchestrator's tool use as an Observation the first time each
        # tool call is seen (register G4). OpenCode runs no orchestrator/subagent
        # split, so every tool is the orchestrator's; a tool part streams several
        # state updates under one id, so the seen-set keeps the status line's tool
        # count one-per-call rather than one-per-update.
        if isinstance(part_id, str) and part_id and part_id not in self.tool_calls_seen:
            self.tool_calls_seen.add(part_id)
            self.emit(ToolObserved(tool if isinstance(tool, str) and tool else "tool"))
        if not isinstance(tool, str) or tool.lower() not in {"question", "askuserquestion"}:
            return
        self.question = extract_question(part.get("state")) or "The backend attempted to ask a question."

    def _accept_step_finish(self, part: dict[str, Any]) -> None:
        # OpenCode reports its token counts on step-finish parts; the orchestrator's
        # live context gauge is the input plus cache-read tokens there (register G5),
        # the per-Backend arithmetic `orchestrator_context` owns. Absent or malformed
        # token metadata emits nothing rather than a zero gauge.
        context = orchestrator_context(part.get("tokens"))
        if context is not None:
            self.emit(ContextObserved(context))

    def _session(self, value: Any) -> None:
        if not isinstance(value, str):
            return
        if self.session_id is None:
            self.session_id = value
        elif value != self.session_id:
            raise RalphError("OpenCode stream contained inconsistent session metadata")

    def _accept_assistant_route(self, provider: str, model: str) -> None:
        route = f"{provider}/{model}"
        if provider != "openai" or not model:
            raise RalphError("OpenCode used an alternate or malformed provider route")
        if not self.assistant_models and route != self.expected_model:
            raise RalphError("OpenCode initial model did not match the selected model")
        self.assistant_models.append(route)

    @property
    def fallback_models(self) -> list[str]:
        return list(dict.fromkeys(model for model in self.assistant_models if model != self.expected_model))

    @property
    def final_text(self) -> str:
        if not self.assistant_messages:
            return ""
        message_id = self.assistant_messages[-1]
        return "".join(text for owner, text in self.parts.values() if owner == message_id)


def export_session(
    worktree: Path,
    session_id: str,
    env: dict[str, str],
    timeout: float | None,
) -> str:
    # An unlinked temporary file, never a pipe and never a named artifact: the
    # export is raw backend output, so it must not be reachable by name the way
    # every redacted `write_json` artifact is, and no crash can leave it behind.
    try:
        handle = tempfile.TemporaryFile()
    except OSError as error:
        raise RalphError(f"cannot capture the OpenCode session export: {error.strerror}") from None
    with handle:
        try:
            process = subprocess.Popen(
                ["opencode", "--pure", "export", session_id],
                cwd=worktree,
                env=env,
                stdout=handle,
                # Nothing reads this export's stderr, and a pipe is the very
                # hazard being avoided, so there is no drain obligation at all.
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as error:
            raise RalphError(f"cannot run opencode: {error.strerror}") from None
        controller = ProcessController(process, timeout or 0)
        controller.start()
        try:
            try:
                process.communicate(timeout=controller.remaining())
            except subprocess.TimeoutExpired:
                controller.timed_out = True
                controller.stop_gracefully()
                process.communicate()
            if controller.timed_out:
                raise TimeoutError
            if controller.interrupted:
                raise HandoffError(
                    "OpenCode iteration interrupted by user",
                    session_id,
                    outcome="interrupted",
                )
        finally:
            if process.poll() is None or controller.group_alive():
                controller.stop_gracefully()
            controller.finish()
        if process.returncode:
            raise RalphError("opencode session export failed")
        try:
            handle.seek(0)
            return handle.read().decode("utf-8")
        except UnicodeDecodeError:
            raise RalphError("OpenCode session export contained invalid UTF-8") from None
        except OSError as error:
            raise RalphError(f"cannot read the OpenCode session export: {error.strerror}") from None


def verify_session(
    worktree: Path,
    run_dir: Path,
    session_id: str,
    model: str,
    env: dict[str, str],
    timeout: float | None,
    runtime_result: EventResult,
) -> str:
    if timeout is not None and timeout <= 0:
        raise TimeoutError
    deadline = time.monotonic() + timeout if timeout is not None else None
    exported = export_session(worktree, session_id, env, timeout)
    try:
        data = json.loads(exported)
    except json.JSONDecodeError:
        # Kept apart from the metadata contract below: a payload that is not JSON
        # at all is a broken read or a broken export, not OpenCode reporting a
        # session Ralph refuses to accept, and conflating the two sends the
        # operator hunting through routing metadata that was never in question.
        raise RalphError("OpenCode session export was not valid JSON") from None
    try:
        messages = data["messages"]
        if not isinstance(messages, list):
            raise TypeError
        assistants = [
            item
            for item in messages
            if isinstance(item, dict)
            and isinstance(item.get("info"), dict)
            and item["info"].get("role") == "assistant"
        ]
        routes: list[str] = []
        for item in assistants:
            info = item["info"]
            if info.get("sessionID") != session_id:
                raise TypeError
            provider = info.get("providerID")
            message_model = info.get("modelID")
            if provider != "openai" or not isinstance(message_model, str) or not message_model:
                raise TypeError
            routes.append(f"{provider}/{message_model}")
        active_model = routes[0]
        parts = assistants[-1]["parts"]
        if not isinstance(parts, list):
            raise TypeError
        final_text = "".join(
            part.get("text", "")
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        )
    except (KeyError, IndexError, TypeError):
        raise RalphError("OpenCode session export omitted required metadata") from None
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError
    if active_model != model:
        raise RalphError("OpenCode initial model did not match the selected model")
    if not final_text:
        raise RalphError("OpenCode session export omitted the final assistant result")
    data["ralph_verification"] = {
        "assistant_models": routes,
        "fallback_models": list(
            dict.fromkeys(
                route
                for route in runtime_result.assistant_models + routes
                if route != model
            )
        ),
        "initial_model": active_model,
        "session_id": session_id,
    }
    write_json(run_dir / "session.json", data)
    return final_text


def resume_argv(worktree: Path, model: str, session: str) -> list[str]:
    # The interactive OpenCode command a handed-off session resumes into, minus the
    # Launch chain wrap that ``launch.session_argv`` adds around it.
    return [
        "opencode",
        "--pure",
        "--model",
        model,
        "--auto",
        "--session",
        session,
        "--dir",
        str(worktree),
    ]


def execute_iteration(
    worktree: Path,
    run_dir: Path,
    prompt: str,
    model: str,
    env: dict[str, str],
    timeout: float,
    sandbox_profile: Path | None = None,
    observe: "ObservationSink | None" = None,
) -> tuple[str, str | None, str | None]:
    # The injected Observation sink (the Run console): the accumulator emits the
    # orchestrator's tool use and live context gauge through it so the status line
    # means the same on an OpenCode run as on a Claude one (register G4/G5, #41).
    stdout_path = run_dir / "stdout.ndjson"
    stderr_path = run_dir / "stderr.log"
    args = session_argv(
        [
            "opencode",
            "--pure",
            "run",
            "--model",
            model,
            "--format",
            "json",
            "--auto",
            "--dir",
            str(worktree),
        ],
        sandbox_profile,
    )
    result = EventResult(model, observe)
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
        raise RalphError(f"could not start OpenCode: {error.strerror}") from None

    controller = ProcessController(process, timeout)
    controller.start()
    try:
        return _consume_opencode_iteration(
            process,
            controller,
            result,
            worktree,
            run_dir,
            prompt,
            model,
            env,
            stdout_path,
            stderr_path,
        )
    finally:
        if process.poll() is None or controller.group_alive():
            controller.stop_gracefully()
        controller.finish()


def _consume_opencode_iteration(
    process: subprocess.Popen[str],
    controller: ProcessController,
    result: EventResult,
    worktree: Path,
    run_dir: Path,
    prompt: str,
    model: str,
    env: dict[str, str],
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
    try:
        process.stdin.write(prompt + active_protocol())
        process.stdin.close()
    except BrokenPipeError:
        process.stdout.close()
        process.wait()
        thread.join()
        raise_if_controlled_stop(controller, "OpenCode", result.session_id)
        raise RalphError("OpenCode exited before accepting the prompt") from None
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
                write_opencode_session(run_dir, result)
                # A stopped backend can truncate its final output mid-character;
                # blame Ralph's own timeout or interrupt before the contract.
                raise_if_controlled_stop(controller, "OpenCode", result.session_id)
                raise_backend_contract_failure(
                    result.session_id, "OpenCode emitted invalid UTF-8 output"
                )
            retained.write(redact(line))
            retained.flush()
            try:
                result.accept(json.loads(line))
            except (json.JSONDecodeError, RecursionError):
                # RecursionError comes from JSON nested past the interpreter
                # limit (or a pathologically deep question payload); treat it as
                # malformed output and fail closed rather than let it escape as a
                # traceback past every handler.
                controller.force_kill()
                thread.join()
                write_opencode_session(run_dir, result)
                raise_if_controlled_stop(controller, "OpenCode", result.session_id)
                if result.session_id:
                    raise HandoffError(
                        "OpenCode emitted malformed structured output",
                        result.session_id,
                        outcome="backend_contract_failure",
                    ) from None
                raise RalphError("OpenCode emitted malformed structured output") from None
            except RalphError as error:
                controller.stop_gracefully()
                thread.join()
                write_opencode_session(run_dir, result)
                # A backend Ralph itself stopped may close its stream with an
                # error event; report the timeout or interruption rather than
                # misclassifying that artifact as a contract failure.
                raise_if_controlled_stop(controller, "OpenCode", result.session_id)
                if result.session_id:
                    raise HandoffError(
                        str(error),
                        result.session_id,
                        outcome="backend_contract_failure",
                    ) from None
                raise StartedIterationError(str(error), "backend_contract_failure") from None
            if result.question:
                controller.stop_gracefully()
                thread.join()
                if not result.session_id:
                    raise RalphError("OpenCode attempted a question before session creation")
                raise HandoffError("OpenCode attempted a native question tool", result.session_id, result.question)
    returncode = process.wait()
    thread.join()
    if stderr_invalid and not (controller.timed_out or controller.interrupted):
        write_opencode_session(run_dir, result)
        raise_backend_contract_failure(
            result.session_id, "OpenCode emitted invalid UTF-8 on stderr"
        )
    if controller.timed_out or controller.interrupted:
        write_json(
            run_dir / "session.json",
            {"final_result_received": False, "session_id": result.session_id},
        )
    raise_if_controlled_stop(controller, "OpenCode", result.session_id)
    if result.backend_error:
        if result.session_id:
            raise HandoffError(
                result.backend_error,
                result.session_id,
                outcome="backend_failure",
            )
        raise StartedIterationError(result.backend_error, "backend_failure")
    if returncode:
        if result.session_id:
            raise HandoffError(
                "OpenCode session failed; see retained stderr",
                result.session_id,
                outcome="backend_failure",
            )
        raise RalphError("OpenCode session failed; see retained stderr")
    if not result.session_id:
        raise RalphError("OpenCode output omitted required session metadata or final result")
    controller.finish()
    try:
        final_text = verify_session(
            worktree,
            run_dir,
            result.session_id,
            model,
            env,
            controller.remaining(),
            result,
        )
    except TimeoutError:
        controller.timed_out = True
        raise HandoffError(
            "OpenCode iteration timed out",
            result.session_id,
            outcome="timeout",
        ) from None
    except HandoffError:
        raise
    except RalphError as error:
        raise HandoffError(
            str(error),
            result.session_id,
            outcome="backend_contract_failure",
        ) from None
    explicit = explicit_needs_input(final_text)
    if explicit:
        raise HandoffError("OpenCode requested operator input", result.session_id, explicit)
    inferred = inferred_needs_input(final_text)
    if inferred:
        # An unmarked concluding question is a low-confidence signal; the loop must
        # not take the irreversible operator-halt on a guess. Emit the fact (the raw
        # fragment) through the Observation sink and let the Run console redact, bound,
        # and word the warning -- a bounded interruption, never an outlet for the whole
        # final message -- so this adapter constructs no operator-facing text (#39/#41,
        # register G14/G17/G19). The next iteration re-derives from the tracker.
        result.emit(UnmarkedQuestion(inferred))
    complete = has_completion_marker(final_text)
    # The concluding message rides back with the outcome so the Run console can show
    # it in the Iteration's outcome block; it is the same final message the marker was
    # read from. The Loop truncates it for display only (register G14).
    # The outcome names what happened to *this Iteration*, never to the run: an
    # Iteration that ended without a completion marker is ``incomplete``, which is a
    # normal end of iteration under the Loop protocol, and only the Loop can say
    # whether the budget then ran out (register J3).
    return ("complete" if complete else "incomplete"), result.session_id, final_text


def write_opencode_session(run_dir: Path, result: EventResult) -> None:
    write_json(
        run_dir / "session.json",
        {
            "assistant_models": result.assistant_models,
            "fallback_models": result.fallback_models,
            "final_result_received": False,
            "session_id": result.session_id,
        },
    )
