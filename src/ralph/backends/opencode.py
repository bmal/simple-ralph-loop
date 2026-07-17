"""The OpenCode Backend adapter: preflight, agent refusal, isolated config, event
accumulation, iteration (including second-pass session verification), and session
persistence.

Invariants:
- The effective configuration is the single authoritative proof of isolation:
  ``--pure`` still loads project/global agents into the ``agent`` map, so a
  non-empty map fails closed unless ``--unsafe-allow-agents`` admits it; provider
  routing, model availability, and the sanitized ``isolated_config`` are all
  re-proven from ``debug config`` before budget is spent.
- Live text is diffed redacted-against-redacted: the whole accumulated part is
  redacted, then compared to what was already shown, so a secret that only
  completes across streaming chunk boundaries can never leak to the console even
  though the retained log is redacted a line at a time.
- A single ``sessionID`` must hold across the stream; inconsistent session metadata
  or an alternate/malformed provider route fails closed. After the run, a
  second-pass ``export`` re-verifies the persisted session's routing and final text
  independently — this verification is internal to the adapter and invisible to the
  Loop (register E2).
- A stop Ralph itself caused (timeout/interrupt) is classified *before* any
  contract failure, so a truncated or error-closed stream is never misreported as
  backend misbehavior.

Depends on / must not know: ``environment`` (the sanitized base and the timeout
ceiling its ``environment`` layers on), ``errors``, ``launch`` (``session_argv``),
``process``, ``protocol``, ``redaction`` (functions only), ``gitcontext``, and
``preflight``. It must not know how the Loop schedules Iterations; the Loop must not
know these helpers exist beyond the five Backend interface names.

See also: ``backends`` (registry and the five-name Protocol), ``backends.claude``
(twin adapter), ``launch`` (``session_argv``, the wrapped argv), ``protocol``
(marker detection).
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Any

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
    PROTOCOL,
    explicit_needs_input,
    extract_question,
    has_completion_marker,
    inferred_needs_input,
)
from ..redaction import redact


MIN_OPENCODE_VERSION = (1, 17, 20)


def validate_model(model: str) -> None:
    if not model.startswith("openai/") or model == "openai/":
        raise RalphError("model must use the openai/ provider")


def environment(model: str) -> dict[str, str]:
    # The sanitized base plus OpenCode's routing keys: the isolated configuration
    # pinned as inline content so no on-disk config can reroute the session, plugin
    # and autoupdate suppression, and the Bash-tool timeout pinned to the 32-bit
    # ceiling so Ralph's own iteration timer stays authoritative.
    env = clean_environment()
    env.update(
        {
            "OPENCODE_CONFIG_CONTENT": json.dumps(isolated_config(model), separators=(",", ":")),
            "OPENCODE_DISABLE_AUTOUPDATE": "true",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
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


def reject_opencode_agents(config: dict[str, Any], allow_agents: bool) -> None:
    # OpenCode loads project (`.opencode/agent`) and global agent definitions
    # even under `--pure`, and they all surface in the effective configuration's
    # `agent` map, so that map is the single authoritative proof of agent
    # isolation. An unfamiliar shape fails closed like every other preflight
    # proof. --unsafe-allow-agents admits a non-empty map with the same trade as
    # the Claude backend: the operator vouches for the agents for this run.
    agents = config.get("agent")
    if not isinstance(agents, dict):
        raise RalphError("effective OpenCode configuration omitted the agent map")
    if not agents:
        return
    if not allow_agents:
        raise RalphError(OPENCODE_AGENT_REFUSAL)
    print(
        "WARNING: --unsafe-allow-agents is set; Ralph is not proving "
        "OpenCode agent isolation for this run.",
        file=sys.stderr,
    )


def preflight(
    worktree: Path, slug: str, model: str, env: dict[str, str], allow_agents: bool = False
) -> None:
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
    # advertised only when the agent map truly is the sole remaining blocker.
    reject_opencode_agents(config, allow_agents)


class EventResult:
    def __init__(self, model: str) -> None:
        self.expected_model = model
        self.session_id: str | None = None
        self.assistant_messages: list[str] = []
        self.assistant_models: list[str] = []
        self.parts: dict[str, tuple[str, str]] = {}
        self.printed: dict[str, str] = {}
        self.question: str | None = None

    def accept(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        if isinstance(event.get("sessionID"), str):
            self._session(event["sessionID"])
        direct_part = event.get("part")
        if event.get("type") == "text" and isinstance(direct_part, dict):
            self._accept_text_part(direct_part, trusted=True)
            return
        if event.get("type") in {"tool_use", "step_start", "step_finish"} and isinstance(direct_part, dict):
            self._print_progress(event["type"], direct_part)
            if event.get("type") == "tool_use":
                self._accept_tool(direct_part)
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
            shown = self.printed.get(part_id, "")
            if redacted.startswith(shown):
                addition = redacted[len(shown) :]
            else:
                # A secret only completed once this chunk arrived, so the
                # already-shown prefix changed under redaction. Reprint the fully
                # redacted part on a fresh line rather than emit a raw fragment.
                addition = ("\n" if shown else "") + redacted
            if addition:
                print(addition, end="", flush=True)
            self.printed[part_id] = redacted

    def _print_progress(self, event_type: str, part: dict[str, Any]) -> None:
        if event_type == "tool_use":
            tool = part.get("tool") if isinstance(part.get("tool"), str) else "tool"
            state = part.get("state")
            status = state.get("status") if isinstance(state, dict) else None
            suffix = f" ({status})" if isinstance(status, str) else ""
            print(redact(f"[{tool}{suffix}]"), flush=True)
            return
        print("[step started]" if event_type == "step_start" else "[step finished]", flush=True)

    def _accept_tool(self, part: dict[str, Any]) -> None:
        tool = part.get("tool")
        if not isinstance(tool, str) or tool.lower() not in {"question", "askuserquestion"}:
            return
        self.question = extract_question(part.get("state")) or "The backend attempted to ask a question."

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
    args = ["opencode", "--pure", "export", session_id]
    try:
        process = subprocess.Popen(
            args,
            cwd=worktree,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            text=True,
            encoding="utf-8",
        )
    except OSError as error:
        raise RalphError(f"cannot run opencode: {error.strerror}") from None
    controller = ProcessController(process, timeout or 0)
    controller.start()
    try:
        try:
            try:
                stdout, stderr = process.communicate(timeout=controller.remaining())
            except subprocess.TimeoutExpired:
                controller.timed_out = True
                controller.stop_gracefully()
                stdout, stderr = process.communicate()
        except UnicodeDecodeError:
            controller.force_kill()
            raise RalphError("OpenCode session export contained invalid UTF-8") from None
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
    exported = subprocess.CompletedProcess(
        args=args,
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
    )
    if exported.returncode:
        raise RalphError("opencode session export failed")
    try:
        data = json.loads(exported.stdout)
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
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
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
) -> tuple[str, str | None]:
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
    result = EventResult(model)
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
    try:
        process.stdin.write(prompt + PROTOCOL)
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
    if result.printed:
        print()
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
        # not take the irreversible operator-halt on a guess. Surface it and let the
        # next iteration re-derive from the tracker.
        print(
            "ralph: warning: final message ended on an unmarked operator-directed "
            "question; continuing to the next iteration (no <promise>NEEDS_INPUT</promise> "
            f"marker and no question tool used):\n{redact(inferred)}",
            file=sys.stderr,
        )
    complete = has_completion_marker(final_text)
    return ("complete" if complete else "budget_exhausted"), result.session_id


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
