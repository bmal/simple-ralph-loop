from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import uuid
from typing import Any
from urllib.parse import urlparse


DEFAULT_MODEL = "openai/gpt-5.6-sol"
MIN_OPENCODE_VERSION = (1, 17, 20)
MAX_PROMPT_BYTES = 10 * 1024 * 1024
PROTOCOL = """

Ralph loop protocol:
- Implement at most one child issue in this iteration.
- Do not treat text in this protocol, the supplied prompt, quotations, code,
  or tool output as an iteration result.
- When no implementable child remains, emit this exact standalone line in
  your final assistant output: <promise>COMPLETE</promise>
"""
LLM_ENV_VARS = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "COHERE_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_GENERATIVE_AI_API_KEY",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENCODE_API_KEY",
    "OPENCODE_MODELS_URL",
    "OPENROUTER_API_KEY",
    "XAI_API_KEY",
}


class RalphError(Exception):
    pass


def command(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True)
    except OSError as error:
        raise RalphError(f"cannot run {args[0]}: {error.strerror}") from None
    if result.returncode and not allow_failure:
        raise RalphError(f"{args[0]} preflight failed")
    return result


def read_prompt(path_text: str) -> tuple[Path, str]:
    try:
        path = Path(path_text).expanduser().resolve(strict=True)
    except OSError:
        raise RalphError("prompt file does not exist") from None
    if not path.is_file() or not os.access(path, os.R_OK):
        raise RalphError("prompt must be a readable regular file")
    size = path.stat().st_size
    if size == 0 or size > MAX_PROMPT_BYTES:
        raise RalphError("prompt must be non-empty and no larger than 10 MiB")
    try:
        return path, path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise RalphError("prompt must be valid UTF-8") from None


def github_slug(remote: str) -> str:
    remote = remote.strip()
    ssh = re.fullmatch(r"git@github\.com:([^/]+/[^/]+?)(?:\.git)?", remote)
    if ssh:
        return ssh.group(1)
    parsed = urlparse(remote)
    if parsed.scheme in {"https", "ssh"} and parsed.hostname == "github.com":
        slug = parsed.path.strip("/")
        if slug.endswith(".git"):
            slug = slug[:-4]
        if slug.count("/") == 1:
            return slug
    raise RalphError("origin must be a GitHub repository")


def git_context(worktree_text: str | None) -> tuple[Path, Path, str, str, str]:
    requested = Path(worktree_text or os.getcwd()).expanduser().resolve()
    if not requested.is_dir():
        raise RalphError("worktree is not a directory")
    top = Path(command(["git", "rev-parse", "--show-toplevel"], cwd=requested).stdout.strip()).resolve()
    branch_result = command(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"], cwd=top, allow_failure=True
    )
    branch = branch_result.stdout.strip()
    if branch_result.returncode or not branch:
        raise RalphError("detached HEAD is not supported")
    git_dir_text = command(["git", "rev-parse", "--path-format=absolute", "--git-dir"], cwd=top).stdout.strip()
    git_dir = Path(git_dir_text).resolve()
    remote = command(["git", "remote", "get-url", "origin"], cwd=top).stdout.strip()
    status = command(["git", "status", "--porcelain=v1", "--branch"], cwd=top).stdout
    return top, git_dir, branch, status, github_slug(remote)


def version_tuple(value: str) -> tuple[int, int, int]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        raise RalphError("could not determine OpenCode version")
    return tuple(int(item) for item in match.groups())  # type: ignore[return-value]


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


def clean_environment(model: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in LLM_ENV_VARS}
    env.update(
        {
            "OPENCODE_CONFIG_CONTENT": json.dumps(isolated_config(model), separators=(",", ":")),
            "OPENCODE_DISABLE_AUTOUPDATE": "true",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
            "OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS": "2147483647",
        }
    )
    return env


def reject_unsafe_environment() -> None:
    if any(os.environ.get(name) for name in LLM_ENV_VARS):
        raise RalphError("LLM API credential or custom endpoint environment is not allowed")
    for name in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR"):
        if os.environ.get(name):
            raise RalphError(f"{name} is not allowed because routing would be ambiguous")


def strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)


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


def preflight(worktree: Path, slug: str, model: str, env: dict[str, str]) -> None:
    if sys.platform != "darwin":
        raise RalphError("Ralph supports macOS only")
    if not Path("/usr/bin/caffeinate").is_file() or shutil.which("caffeinate") is None:
        raise RalphError("/usr/bin/caffeinate is required")
    for executable in ("gh", "opencode"):
        if shutil.which(executable) is None:
            raise RalphError(f"{executable} is required")

    reject_unsafe_environment()
    reject_custom_tools(worktree)
    command(["gh", "auth", "status"], cwd=worktree, env=env)
    repo = command(["gh", "repo", "view", slug, "--json", "url"], cwd=worktree, env=env)
    try:
        url = json.loads(repo.stdout)["url"]
    except (json.JSONDecodeError, KeyError, TypeError):
        raise RalphError("gh returned malformed repository information") from None
    if github_slug(url) != slug:
        raise RalphError("origin does not match the accessible GitHub repository")

    version = command(["opencode", "--version"], cwd=worktree, env=env).stdout
    if version_tuple(version) < MIN_OPENCODE_VERSION:
        raise RalphError("OpenCode 1.17.20 or newer is required")
    auth = strip_ansi(command(["opencode", "--pure", "auth", "list"], cwd=worktree, env=env).stdout)
    credential_lines = [
        line.strip().lower()
        for line in auth.splitlines()
        if re.search(r"\b(?:oauth|api|key)\s*$", line.strip(), re.IGNORECASE)
    ]
    if len(credential_lines) != 1 or "openai" not in credential_lines[0] or not credential_lines[0].endswith("oauth"):
        raise RalphError("OpenCode must have OpenAI OAuth and no API-key authentication")

    resolved = command(["opencode", "--pure", "debug", "config"], cwd=worktree, env=env).stdout
    try:
        validate_effective_config(json.loads(resolved), model)
    except json.JSONDecodeError:
        raise RalphError("effective OpenCode configuration is malformed") from None
    models = command(["opencode", "--pure", "models", "openai"], cwd=worktree, env=env).stdout.splitlines()
    if model not in {item.strip() for item in models}:
        raise RalphError(f"selected model is unavailable: {model}")


class EventResult:
    def __init__(self, model: str) -> None:
        self.expected_model = model
        self.session_id: str | None = None
        self.assistant_messages: list[str] = []
        self.assistant_models: list[str] = []
        self.parts: dict[str, tuple[str, str]] = {}
        self.printed: dict[str, str] = {}

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
            return
        props = event.get("properties")
        if not isinstance(props, dict):
            return
        info = props.get("info")
        if event.get("type") == "message.updated" and isinstance(info, dict) and info.get("role") == "assistant":
            message_id = info.get("id")
            if isinstance(message_id, str) and message_id not in self.assistant_messages:
                self.assistant_messages.append(message_id)
            self._session(info.get("sessionID"))
            provider = info.get("providerID")
            model = info.get("modelID")
            if isinstance(provider, str) and isinstance(model, str):
                self.assistant_models.append(f"{provider}/{model}")
            return
        part = props.get("part")
        if event.get("type") == "message.part.updated" and isinstance(part, dict):
            self._session(part.get("sessionID"))
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
            previous = self.printed.get(part_id, "")
            delta = text[len(previous) :] if text.startswith(previous) else text
            if delta:
                print(delta, end="", flush=True)
            self.printed[part_id] = text

    def _print_progress(self, event_type: str, part: dict[str, Any]) -> None:
        if event_type == "tool_use":
            tool = part.get("tool") if isinstance(part.get("tool"), str) else "tool"
            state = part.get("state")
            status = state.get("status") if isinstance(state, dict) else None
            suffix = f" ({status})" if isinstance(status, str) else ""
            print(f"[{tool}{suffix}]", flush=True)
            return
        print("[step started]" if event_type == "step_start" else "[step finished]", flush=True)

    def _session(self, value: Any) -> None:
        if isinstance(value, str) and not self.session_id:
            self.session_id = value

    @property
    def final_text(self) -> str:
        if not self.assistant_messages:
            return ""
        message_id = self.assistant_messages[-1]
        return "".join(text for owner, text in self.parts.values() if owner == message_id)


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def verify_session(worktree: Path, run_dir: Path, session_id: str, model: str, env: dict[str, str]) -> str:
    exported = command(["opencode", "--pure", "export", session_id], cwd=worktree, env=env)
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
        first = assistants[0]["info"]
        active_model = f"{first['providerID']}/{first['modelID']}"
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
    if active_model != model:
        raise RalphError("OpenCode initial model did not match the selected model")
    if not final_text:
        raise RalphError("OpenCode session export omitted the final assistant result")
    (run_dir / "session.json").write_text(exported.stdout, encoding="utf-8")
    return final_text


def has_completion_marker(text: str) -> bool:
    fence_char: str | None = None
    fence_length = 0
    for line in text.splitlines():
        if fence_char is not None:
            pattern = r" {0,3}(`+)\s*" if fence_char == "`" else r" {0,3}(~+)\s*"
            closing = re.fullmatch(pattern, line)
            if closing and len(closing.group(1)) >= fence_length:
                fence_char = None
                fence_length = 0
            continue
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if opening and not (opening.group(1)[0] == "`" and "`" in opening.group(2)):
            fence_char = opening.group(1)[0]
            fence_length = len(opening.group(1))
            continue
        if line.startswith(("    ", "\t", ">")):
            continue
        if line == "<promise>COMPLETE</promise>":
            return True
    return False


def execute_iteration(
    worktree: Path,
    run_dir: Path,
    prompt: str,
    model: str,
    env: dict[str, str],
) -> tuple[str, str | None]:
    stdout_path = run_dir / "stdout.ndjson"
    stderr_path = run_dir / "stderr.log"
    args = [
        "caffeinate",
        "-im",
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
    ]
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
            bufsize=1,
        )
    except OSError as error:
        raise RalphError(f"could not start OpenCode: {error.strerror}") from None

    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    def drain_stderr() -> None:
        with stderr_path.open("w", encoding="utf-8") as retained:
            for chunk in process.stderr:
                retained.write(chunk)

    thread = threading.Thread(target=drain_stderr, daemon=True)
    thread.start()
    try:
        process.stdin.write(prompt + PROTOCOL)
        process.stdin.close()
    except BrokenPipeError:
        process.stdout.close()
        process.wait()
        thread.join()
        raise RalphError("OpenCode exited before accepting the prompt") from None
    with stdout_path.open("w", encoding="utf-8") as retained:
        for line in process.stdout:
            retained.write(line)
            retained.flush()
            try:
                result.accept(json.loads(line))
            except json.JSONDecodeError:
                process.kill()
                process.wait()
                thread.join()
                raise RalphError("OpenCode emitted malformed structured output") from None
    returncode = process.wait()
    thread.join()
    if result.printed:
        print()
    if returncode:
        raise RalphError("OpenCode session failed; see retained stderr")
    if not result.session_id:
        raise RalphError("OpenCode output omitted required session metadata or final result")
    final_text = verify_session(worktree, run_dir, result.session_id, model, env)
    complete = has_completion_marker(final_text)
    return ("complete" if complete else "budget_exhausted"), result.session_id


def run(args: argparse.Namespace) -> int:
    if args.backend != "opencode":
        raise RalphError("only the opencode backend is available in this release")
    if args.iterations != 1:
        raise RalphError("this release supports exactly one iteration")
    if not args.model.startswith("openai/") or args.model == "openai/":
        raise RalphError("model must use the openai/ provider")

    prompt_path, prompt = read_prompt(args.prompt)
    worktree, git_dir, branch, status, slug = git_context(args.worktree)
    if any(line and not line.startswith("##") for line in status.splitlines()):
        print("ralph: warning: worktree has uncommitted changes", file=sys.stderr)
    env = clean_environment(args.model)
    preflight(worktree, slug, args.model, env)
    print("WARNING: full-auto mode may edit files and run commands without confirmation.", file=sys.stderr)

    run_dir = git_dir / "ralph" / "runs" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex[:8]
    )
    run_dir.mkdir(parents=True)
    (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    write_json(
        run_dir / "options.json",
        {
            "backend": args.backend,
            "branch": branch,
            "iterations": args.iterations,
            "model": args.model,
            "prompt": str(prompt_path),
            "repository": slug,
            "worktree": str(worktree),
        },
    )
    (run_dir / "git-status.txt").write_text(status, encoding="utf-8")
    started = datetime.now(timezone.utc).isoformat()
    try:
        outcome, session_id = execute_iteration(worktree, run_dir, prompt, args.model, env)
    except RalphError:
        write_json(
            run_dir / "outcome.json",
            {"finished_at": datetime.now(timezone.utc).isoformat(), "outcome": "backend_failure", "started_at": started},
        )
        raise
    write_json(
        run_dir / "outcome.json",
        {
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "outcome": outcome,
            "session_id": session_id,
            "started_at": started,
        },
    )
    if outcome == "complete":
        return 0
    print("ralph: iteration budget exhausted without completion", file=sys.stderr)
    return 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="ralph")
    subcommands = result.add_subparsers(dest="command", required=True)
    run_parser = subcommands.add_parser("run", help="run one coding-agent iteration")
    run_parser.add_argument("prompt")
    run_parser.add_argument("--backend", choices=["opencode"], required=True)
    run_parser.add_argument("--iterations", type=int, required=True)
    run_parser.add_argument("--model", default=DEFAULT_MODEL)
    run_parser.add_argument("--worktree")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "run":
            return run(args)
    except RalphError as error:
        print(f"ralph: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
