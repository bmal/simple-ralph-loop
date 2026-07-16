from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from typing import Any
from urllib.parse import urlparse


DEFAULT_MODELS = {
    "claude": "claude-opus-4-8",
    "opencode": "openai/gpt-5.6-sol",
}
MIN_OPENCODE_VERSION = (1, 17, 20)
MIN_CLAUDE_VERSION = (2, 1, 208)
MAX_PROMPT_BYTES = 10 * 1024 * 1024
# Absolute path to the macOS sleep-assertion tool. It is invoked by absolute
# path (never resolved through PATH) so a repository-local or otherwise
# shadowed `caffeinate` cannot silently replace the real one. RALPH_CAFFEINATE
# is an internal test seam that lets the suite substitute a fake; production
# runs never set it and always use the system binary.
DEFAULT_CAFFEINATE = "/usr/bin/caffeinate"
# Host locations consulted to detect MDM-managed Claude configuration. Both are
# absolute system paths in production; dedicated test seams (see
# claude_managed_root / claude_profiles_executable) let the suite isolate the
# checks from real host state without weakening the production defaults.
DEFAULT_CLAUDE_MANAGED_ROOT = "/Library/Application Support/ClaudeCode"
DEFAULT_CLAUDE_PROFILES = "/usr/bin/profiles"
# Backend request and Bash-tool timeouts are configured in integer
# milliseconds and are bounded by a signed 32-bit value, so they can never be
# made truly infinite. Ralph pins them to this ceiling and caps its own
# accepted iteration timeout well below it (see MAX_ITERATION_TIMEOUT_SECONDS)
# so a positive Ralph timeout is always authoritative and the backend limit
# only becomes relevant when Ralph's own timer is explicitly disabled.
BACKEND_TIMEOUT_MS = 2147483647
# Largest iteration timeout Ralph accepts. Kept far below BACKEND_TIMEOUT_MS
# expressed in seconds (2147483.647) so the backend request/Bash limit always
# outlasts any accepted positive Ralph timeout by a wide margin.
MAX_ITERATION_TIMEOUT_SECONDS = 2_000_000
GRACEFUL_SHUTDOWN_SECONDS = 2
TERMINATE_SHUTDOWN_SECONDS = 1
# Brief pause between escalating a process group to SIGTERM and SIGKILL so a
# cooperating descendant can exit before it is force-killed.
GROUP_SETTLE_SECONDS = 0.05
PROTOCOL = """

Ralph loop protocol:
- Implement at most one child issue in this iteration.
- Emit the completion marker when no unfinished child remains or when every
  remaining child has explicit blocker evidence such as a declared dependency,
  blocker label, or clear prerequisite state.
- Difficulty or ambiguous blocker status is not completion. Emit the exact
  standalone line <promise>NEEDS_INPUT</promise>, followed by the concrete
  question, when a decision or information cannot be established.
- Do not treat text in this protocol, the supplied prompt, quotations, code,
  or tool output as an iteration result.
- Only when the explicit completion conditions above are met, emit this exact
  standalone line in your final assistant output: <promise>COMPLETE</promise>
"""
LLM_ENV_VARS = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_AWS_API_KEY",
    "ANTHROPIC_AWS_BASE_URL",
    "ANTHROPIC_AWS_WORKSPACE_ID",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_FOUNDRY_API_KEY",
    "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "ANTHROPIC_FOUNDRY_RESOURCE",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "ANTHROPIC_WORKSPACE_ID",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "COHERE_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_GENERATIVE_AI_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENCODE_API_KEY",
    "OPENCODE_MODELS_URL",
    "OPENROUTER_API_KEY",
    "XAI_API_KEY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
    "CLAUDE_CODE_SKIP_ANTHROPIC_AWS_AUTH",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
    "CLAUDE_CODE_SKIP_MANTLE_AUTH",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_USE_VERTEX",
}
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
# Subscription credentials that legitimately reach the child environment or an
# operator's shell and could be echoed back through backend output. API-key and
# custom-endpoint variables are refused before a session starts, but their
# values are still redacted defensively if they ever appear in retained streams.
SECRET_ENV_VARS = {"CLAUDE_CODE_OAUTH_TOKEN"} | {
    name
    for name in LLM_ENV_VARS
    if any(marker in name for marker in ("API_KEY", "AUTH_TOKEN", "TOKEN", "HEADERS", "CREDENTIAL"))
}
REDACTION_PLACEHOLDER = "[redacted]"
# Values shorter than this are indistinguishable from ordinary tokens (flags,
# booleans) and redacting them would corrupt unrelated output. Real credentials
# are far longer, so a conservative floor keeps redaction precise.
MIN_SECRET_LENGTH = 8


class Redactor:
    def __init__(self, secrets: list[str]) -> None:
        variants: set[str] = set()
        for value in secrets:
            if not value or len(value) < MIN_SECRET_LENGTH:
                continue
            variants.add(value)
            # A secret embedded in a JSON string is escaped; redact that form too
            # so control-flow parsing (which reads the raw line) stays intact.
            escaped = json.dumps(value)[1:-1]
            if escaped != value:
                variants.add(escaped)
        self._variants = sorted(variants, key=len, reverse=True)

    def scrub(self, text: str) -> str:
        if not self._variants or not text:
            return text
        for variant in self._variants:
            if variant in text:
                text = text.replace(variant, REDACTION_PLACEHOLDER)
        return text

    def __bool__(self) -> bool:
        return bool(self._variants)


_ACTIVE_REDACTOR = Redactor([])


def redact(text: str) -> str:
    return _ACTIVE_REDACTOR.scrub(text)


def collect_secrets() -> list[str]:
    return [os.environ[name] for name in SECRET_ENV_VARS if os.environ.get(name)]


def set_active_redactor(secrets: list[str]) -> None:
    global _ACTIVE_REDACTOR
    _ACTIVE_REDACTOR = Redactor(secrets)


def caffeinate_executable() -> str:
    # Always an absolute path so the sleep assertion cannot be satisfied by a
    # PATH-shadowed executable. RALPH_CAFFEINATE is honored only as a test seam.
    return os.environ.get("RALPH_CAFFEINATE") or DEFAULT_CAFFEINATE


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


class RalphError(Exception):
    pass


class HandoffError(RalphError):
    def __init__(
        self,
        reason: str,
        session_id: str,
        detail: str | None = None,
        outcome: str = "needs_input",
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.session_id = session_id
        self.detail = detail
        self.outcome = outcome


class StartedIterationError(RalphError):
    def __init__(self, reason: str, outcome: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.outcome = outcome


class ProcessController:
    def __init__(self, process: subprocess.Popen[str], timeout: float) -> None:
        self.process = process
        # The child is started in its own session (start_new_session=True), so it
        # leads a process group whose id equals its pid. Capturing the group id
        # once lets us signal the whole tree even after the leader has exited and
        # its pid would otherwise be reaped.
        try:
            self.pgid = os.getpgid(process.pid)
        except (ProcessLookupError, OSError):
            self.pgid = process.pid
        self.timed_out = False
        self.interrupted = False
        self.deadline = time.monotonic() + timeout if timeout else None
        self._timer = threading.Timer(timeout, self._on_timeout) if timeout else None
        if self._timer is not None:
            self._timer.daemon = True
        self._lock = threading.Lock()
        self._interrupt_count = 0
        self._previous_interrupt_handler: Any = None

    def start(self) -> None:
        self._previous_interrupt_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, self._handle_interrupt)
        if self._timer is not None:
            self._timer.start()

    def finish(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            if self._timer is not threading.current_thread():
                self._timer.join()
        if self._previous_interrupt_handler is not None:
            signal.signal(signal.SIGINT, self._previous_interrupt_handler)
            self._previous_interrupt_handler = None

    def _handle_interrupt(self, _signum: int, _frame: Any) -> None:
        self._interrupt_count += 1
        self.interrupted = True
        if self._interrupt_count == 1:
            threading.Thread(target=self.stop_gracefully, daemon=True).start()
        else:
            self.force_kill()

    def _on_timeout(self) -> None:
        # The leader may have exited while a descendant keeps Ralph's pipes open;
        # only treat the timer as spurious when nothing in the group survives.
        if self.process.poll() is not None and not self.group_alive():
            return
        self.timed_out = True
        self.stop_gracefully()

    def group_alive(self) -> bool:
        try:
            os.killpg(self.pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def remaining(self) -> float | None:
        if self.deadline is None:
            return None
        return max(0, self.deadline - time.monotonic())

    def stop_gracefully(self) -> None:
        with self._lock:
            if self.process.poll() is None:
                self._signal_group(signal.SIGINT)
                try:
                    self.process.wait(timeout=GRACEFUL_SHUTDOWN_SECONDS)
                except subprocess.TimeoutExpired:
                    self._signal_group(signal.SIGTERM)
                    try:
                        self.process.wait(timeout=TERMINATE_SHUTDOWN_SECONDS)
                    except subprocess.TimeoutExpired:
                        self.force_kill()
                        return
            # The group can outlive its leader and retain Ralph's pipes, so
            # escalate to terminate any descendants even once the leader is gone.
            self._signal_group(signal.SIGTERM)
            time.sleep(GROUP_SETTLE_SECONDS)
            self._signal_group(signal.SIGKILL)

    def force_kill(self) -> None:
        # Kill the whole group unconditionally: a departed leader can leave
        # pipe-holding descendants that would otherwise block Ralph forever.
        self._signal_group(signal.SIGKILL)
        if self.process.poll() is None:
            self.process.wait()

    def _signal_group(self, requested_signal: signal.Signals) -> None:
        try:
            os.killpg(self.pgid, requested_signal)
        except PermissionError:
            try:
                self.process.send_signal(requested_signal)
            except (ProcessLookupError, OSError):
                pass
        except (ProcessLookupError, OSError):
            pass


def process_identity(pid: int) -> str | None:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
        text=True,
        capture_output=True,
    )
    if result.returncode or not result.stdout.strip():
        return None
    return result.stdout.strip()


def _reject_non_directory(path: Path, info: os.stat_result) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise RalphError(f"Ralph state path is a symlink and will not be used: {path}")
    if not stat.S_ISDIR(info.st_mode):
        raise RalphError(f"Ralph state path is not a directory: {path}")


def secure_state_directory(base: Path, *parts: str) -> Path:
    # Walk each component beneath an already-resolved base directory, creating
    # missing levels and verifying existing ones with lstat so a symlink or
    # unexpected file type anywhere in the chain is refused rather than silently
    # redirecting Ralph state outside the worktree's private Git directory.
    path = base
    for part in parts:
        path = path / part
        try:
            os.mkdir(path)
            continue
        except FileExistsError:
            pass
        except FileNotFoundError:
            raise RalphError(f"Ralph state parent path is missing: {path.parent}") from None
        _reject_non_directory(path, os.lstat(path))
    return path


def read_lock_metadata(path: Path) -> dict[str, Any] | None:
    # Refuse a symlinked or non-regular lock file (it could redirect a write or
    # leak a read). A missing or malformed file is reported as absent metadata:
    # the exclusive flock is the source of truth for mutual exclusion, so a lock
    # file we cannot parse simply carries no ownership claim.
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RalphError("Ralph lock metadata is not a regular file")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


class WorktreeLock:
    def __init__(self, git_dir: Path, metadata_path: Path | None = None) -> None:
        self.git_dir = git_dir
        self.metadata_path = metadata_path
        self.acquired = False
        self.descriptor: int | None = None

    def acquire(self) -> None:
        descriptor: int | None = None
        try:
            descriptor = os.open(self.git_dir, os.O_RDONLY)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if descriptor is not None:
                os.close(descriptor)
            raise RalphError(
                "another Ralph loop is already running in this worktree"
                + self._describe_owner()
            ) from None
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            raise RalphError(f"could not acquire the Ralph worktree lock: {error.strerror}") from None
        if self.metadata_path is not None:
            try:
                # Verify the state root and any pre-existing ownership record
                # before overwriting it. Rejecting a symlinked state root here
                # also keeps the later metadata lstat from being redirected.
                secure_state_directory(self.git_dir, "ralph")
                self._verify_recoverable()
                write_json(
                    self.metadata_path,
                    {"identity": process_identity(os.getpid()), "pid": os.getpid()},
                )
            except RalphError:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
                raise
            except OSError as error:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
                raise RalphError(f"could not write the Ralph worktree lock: {error.strerror}") from None
        self.descriptor = descriptor
        self.acquired = True

    def _describe_owner(self) -> str:
        if self.metadata_path is None:
            return ""
        try:
            data = read_lock_metadata(self.metadata_path)
        except RalphError:
            return ""
        if isinstance(data, dict) and isinstance(data.get("pid"), int):
            return f" (pid {data['pid']})"
        return ""

    def _verify_recoverable(self) -> None:
        # Called while holding the exclusive flock, so no live process holds the
        # lock. A stale, malformed, or reused-PID record is therefore safe to
        # overwrite. The one contradictory case -- a recorded owner that is still
        # alive with a matching process identity -- means the flock guarantee was
        # somehow bypassed, so fail closed rather than clobber a possible loop.
        assert self.metadata_path is not None
        data = read_lock_metadata(self.metadata_path)
        if data is None:
            return
        pid = data.get("pid")
        identity = data.get("identity")
        if not isinstance(pid, int) or pid <= 0:
            return
        current = process_identity(pid)
        if current is not None and isinstance(identity, str) and current == identity:
            raise RalphError(
                "Ralph lock metadata names a live matching owner; refusing to recover it"
            )

    def release(self) -> None:
        if not self.acquired:
            return
        assert self.descriptor is not None
        if self.metadata_path is not None:
            try:
                self.metadata_path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(self.descriptor)
            except OSError:
                pass
            self.descriptor = None
            self.acquired = False

    def __enter__(self) -> WorktreeLock:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


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


def version_tuple(value: str, program: str = "OpenCode") -> tuple[int, int, int]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        raise RalphError(f"could not determine {program} version")
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


def clean_environment(model: str, backend: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in LLM_ENV_VARS}
    ceiling = str(BACKEND_TIMEOUT_MS)
    if backend == "opencode":
        env.update(
            {
                "OPENCODE_CONFIG_CONTENT": json.dumps(isolated_config(model), separators=(",", ":")),
                "OPENCODE_DISABLE_AUTOUPDATE": "true",
                "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
                "OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS": ceiling,
            }
        )
    else:
        env.update(
            {
                "API_TIMEOUT_MS": ceiling,
                "BASH_DEFAULT_TIMEOUT_MS": ceiling,
                "BASH_MAX_TIMEOUT_MS": ceiling,
                "DISABLE_AUTOUPDATER": "1",
            }
        )
    return env


def reject_unsafe_environment() -> None:
    if any(os.environ.get(name) for name in LLM_ENV_VARS):
        raise RalphError("LLM API credential or custom endpoint environment is not allowed")
    for name in ("CLAUDE_CONFIG_DIR", "OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR"):
        if os.environ.get(name):
            raise RalphError(f"{name} is not allowed because routing would be ambiguous")


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


def common_preflight(worktree: Path, slug: str, executable: str, env: dict[str, str]) -> None:
    if sys.platform != "darwin":
        raise RalphError("Ralph supports macOS only")
    if not Path(caffeinate_executable()).is_file():
        raise RalphError("/usr/bin/caffeinate is required")
    for required in ("gh", executable):
        if shutil.which(required) is None:
            raise RalphError(f"{required} is required")

    reject_unsafe_environment()
    command(["gh", "auth", "status"], cwd=worktree, env=env)
    repo = command(["gh", "repo", "view", slug, "--json", "url"], cwd=worktree, env=env)
    try:
        url = json.loads(repo.stdout)["url"]
    except (json.JSONDecodeError, KeyError, TypeError):
        raise RalphError("gh returned malformed repository information") from None
    if github_slug(url) != slug:
        raise RalphError("origin does not match the accessible GitHub repository")


def opencode_preflight(worktree: Path, slug: str, model: str, env: dict[str, str]) -> None:
    common_preflight(worktree, slug, "opencode", env)
    reject_custom_tools(worktree)

    version = command(["opencode", "--version"], cwd=worktree, env=env).stdout
    if version_tuple(version) < MIN_OPENCODE_VERSION:
        raise RalphError("OpenCode 1.17.20 or newer is required")
    auth = command(["opencode", "--pure", "auth", "list"], cwd=worktree, env=env).stdout
    validate_opencode_auth_output(auth)

    resolved = command(["opencode", "--pure", "debug", "config"], cwd=worktree, env=env).stdout
    try:
        validate_effective_config(json.loads(resolved), model)
    except json.JSONDecodeError:
        raise RalphError("effective OpenCode configuration is malformed") from None
    models = command(["opencode", "--pure", "models", "openai"], cwd=worktree, env=env).stdout.splitlines()
    if model not in {item.strip() for item in models}:
        raise RalphError(f"selected model is unavailable: {model}")


CLAUDE_CUSTOMIZATION_DIRS = ("agents", "hooks", "plugins")
# Settings keys that, if present in `.claude/settings.json`, defeat the proof of
# safe isolation. Only `agent` is relaxable via --unsafe-allow-claude-agents.
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
    "--unsafe-allow-claude-agents to admit the .claude/agents directory and the "
    "settings.json 'agent' key for this run (unsafe: Ralph then cannot prove "
    "Claude subagent isolation)"
)


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
    # --unsafe-allow-claude-agents relaxes only the agent vectors: the
    # `.claude/agents` directory and the settings.json `agent` key. It exists for
    # repos whose loop develops or depends on subagents. Hooks, plugins, managed
    # configuration, and every other unsafe setting stay refused, and runtime
    # MCP/plugin/tool isolation is still proven from the init event. The trade is
    # deliberate and unsafe: Ralph can no longer prove which subagents loaded, so
    # the operator vouches for them for this run.
    if allow_agents and (claude_dir / "agents").exists():
        print(
            "WARNING: --unsafe-allow-claude-agents is set; Ralph is not proving "
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


def write_json(path: Path, value: Any) -> None:
    path.write_text(redact(json.dumps(value, indent=2, sort_keys=True)) + "\n", encoding="utf-8")


def record_final_git_state(worktree: Path, run_dir: Path, initial_branch: str) -> str:
    branch_result = command(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"], cwd=worktree, allow_failure=True
    )
    final_branch = branch_result.stdout.strip() or "(detached)"
    status_result = command(
        ["git", "status", "--porcelain=v1", "--branch"], cwd=worktree, allow_failure=True
    )
    status = status_result.stdout or status_result.stderr
    (run_dir / "git-status-final.txt").write_text(status, encoding="utf-8")
    if final_branch != initial_branch:
        print(
            f"ralph: warning: branch changed from {initial_branch} to {final_branch}",
            file=sys.stderr,
        )
    return final_branch


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


def visible_markdown_lines(text: str) -> list[tuple[int, str]]:
    visible: list[tuple[int, str]] = []
    fence_char: str | None = None
    fence_length = 0
    for index, line in enumerate(text.splitlines()):
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
        if line.startswith(("    ", "\t")) or re.match(r"^ {0,3}>", line):
            continue
        visible.append((index, line))
    return visible


def has_completion_marker(text: str) -> bool:
    return any(line == "<promise>COMPLETE</promise>" for _, line in visible_markdown_lines(text))


def extract_question(value: Any) -> str | None:
    if isinstance(value, str) and value.strip().endswith("?"):
        return value.strip()
    if isinstance(value, dict):
        for key in ("question", "questions", "input"):
            found = extract_question(value.get(key))
            if found:
                return found
        for item in value.values():
            found = extract_question(item)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = extract_question(item)
            if found:
                return found
    return None


TOOL_LOG_PREFIXES = ("tool output:", "tool result:", "[tool")
# A trailing courtesy sentence (a sign-off or acknowledgement) that may follow a
# genuine user-directed question in concluding prose. These are stripped before
# deciding whether the conclusion ends on a question so that
# "Should I proceed? Please advise." is still recognized as a handoff.
CLOSING_SENTENCE = re.compile(
    r"(?i)^(?:"
    r"please\b.*"
    r"|thanks?\b.*"
    r"|thank you\b.*"
    r"|(?:kind |best |warm )?regards\b.*"
    r"|cheers\b.*"
    r"|let me know\b.*"
    r"|awaiting\b.*"
    r"|standing by\b.*"
    r"|i(?:'ll| will) wait\b.*"
    r"|i await\b.*"
    r"|your call\b.*"
    r"|up to you\b.*"
    r"|otherwise\b.*"
    r")[.!]*$"
)
# A concluding question is only treated as user-directed when it addresses the
# operator or opens with an interrogative that asks for a decision. This keeps
# the heuristic conservative instead of matching every trailing question mark.
DIRECTED_PRONOUN = re.compile(r"(?i)\b(you|your|yours|i|we|us|me|my|our|ralph)\b")
DIRECTED_OPENER = re.compile(
    r"(?i)^(which|what|whether|should|shall|would|could|can|may|do|does|did|is|are|"
    r"how|when|where|who)\b"
)


def visible_prose_lines(text: str) -> list[tuple[int, str]]:
    visible: list[tuple[int, str]] = []
    in_tool_log = False
    for index, line in visible_markdown_lines(text):
        stripped = line.strip()
        if in_tool_log:
            # A multi-line tool log continues until a blank line separates it
            # from resumed prose, so its inner lines never contribute question
            # text even when they contain question marks.
            if not stripped:
                in_tool_log = False
            visible.append((index, ""))
            continue
        if stripped.lower().startswith(TOOL_LOG_PREFIXES):
            in_tool_log = True
            visible.append((index, ""))
            continue
        without_literals = re.sub(r"`[^`]*`", "", stripped)
        without_literals = re.sub(r"https?://\S+", "", without_literals)
        visible.append((index, without_literals.strip()))
    return visible


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.?!])\s+", text.strip())
    return [part for part in (segment.strip() for segment in parts) if part]


def concluding_question(conclusion: str) -> str | None:
    sentences = split_sentences(conclusion)
    # Drop trailing sign-off sentences so a question followed by a closing line
    # ("Should I proceed? Please advise.") is still detected.
    while sentences and not sentences[-1].endswith("?") and CLOSING_SENTENCE.match(sentences[-1]):
        sentences.pop()
    if not sentences or not sentences[-1].endswith("?"):
        return None
    final = sentences[-1]
    if not DIRECTED_PRONOUN.search(final) and not DIRECTED_OPENER.match(final):
        return None
    return conclusion.strip()


def needs_input_question(text: str) -> str | None:
    visible = visible_prose_lines(text)
    marker_indexes = [
        index
        for index, line in visible_markdown_lines(text)
        if line == "<promise>NEEDS_INPUT</promise>"
    ]
    if marker_indexes:
        marker_index = marker_indexes[-1]
        following = [line for index, line in visible if index > marker_index and line]
        return "\n".join(following) or "The assistant requested operator input."

    paragraphs: list[list[str]] = []
    current: list[str] = []
    for _, line in visible:
        if line:
            current.append(line)
        elif current:
            paragraphs.append(current)
            current = []
    if current:
        paragraphs.append(current)
    if not paragraphs:
        return None
    return concluding_question(" ".join(paragraphs[-1]))


def raise_backend_contract_failure(session_id: str | None, message: str) -> None:
    # A contract failure after a session exists is a resumable, consuming
    # handoff; before any session it is an ordinary pre-session failure.
    if session_id:
        raise HandoffError(message, session_id, outcome="backend_contract_failure")
    raise RalphError(message)


def raise_if_controlled_stop(
    controller: ProcessController,
    backend: str,
    session_id: str | None,
) -> None:
    if not controller.timed_out and not controller.interrupted:
        return
    if controller.timed_out:
        reason = f"{backend} iteration timed out"
        outcome = "timeout"
    else:
        reason = f"{backend} iteration interrupted by user"
        outcome = "interrupted"
    if session_id:
        raise HandoffError(reason, session_id, outcome=outcome)
    raise StartedIterationError(f"{reason} before session metadata was received", outcome)


def execute_opencode_iteration(
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
    question = needs_input_question(final_text)
    if question:
        raise HandoffError("OpenCode requested operator input", result.session_id, question)
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


def execute_iteration(
    backend: str,
    worktree: Path,
    run_dir: Path,
    prompt: str,
    model: str,
    env: dict[str, str],
    timeout: float,
) -> tuple[str, str | None]:
    if backend == "claude":
        return execute_claude_iteration(worktree, run_dir, prompt, model, env, timeout)
    return execute_opencode_iteration(worktree, run_dir, prompt, model, env, timeout)


def shell_command(args: list[str], worktree: Path) -> str:
    return f"cd {shlex.quote(str(worktree))} && {shlex.join(args)}"


def resume_command(
    backend: str, model: str, worktree: Path, session_id: str, allow_agents: bool = False
) -> str:
    # A dedicated `ralph resume` re-establishes the full subscription trust
    # boundary (sanitized environment, per-session OAuth/routing proof, isolated
    # configuration, full-auto permissions, and caffeinate) before handing an
    # operator the interactive backend. A raw backend command would inherit the
    # operator's ambient environment and skip that proof, so recovery is routed
    # through Ralph itself. --session is placed last so callers can rely on it.
    args = [
        "ralph",
        "resume",
        "--backend",
        backend,
        "--model",
        model,
        "--worktree",
        str(worktree),
    ]
    # Reproduce the relaxed check so the handoff can re-prove the same boundary;
    # without it resume would refuse the very agents the run was allowed.
    if allow_agents:
        args.append("--unsafe-allow-claude-agents")
    args += ["--session", session_id]
    return shell_command(args, worktree)


def restart_command(
    backend: str,
    model: str,
    worktree: Path,
    prompt_path: Path,
    remaining: int,
    timeout: float,
    allow_agents: bool = False,
) -> str:
    args = [
        "ralph",
        "run",
        str(prompt_path),
        "--backend",
        backend,
        "--iterations",
        str(remaining),
        "--model",
        model,
        "--worktree",
        str(worktree),
        "--timeout",
        str(timeout),
    ]
    if allow_agents:
        args.append("--unsafe-allow-claude-agents")
    return shell_command(args, worktree)


def print_handoff(
    *,
    reason: str,
    session_id: str | None,
    detail: str | None,
    backend: str,
    model: str,
    worktree: Path,
    prompt_path: Path,
    remaining: int,
    run_id: str,
    timeout: float,
    allow_agents: bool = False,
) -> None:
    terminal = sys.stderr.isatty()
    if terminal:
        print("\a\033[1;31m", end="", file=sys.stderr)
    print("========== RALPH NEEDS OPERATOR ==========", file=sys.stderr)
    print(f"reason: {redact(reason)}", file=sys.stderr)
    print(f"ralph run: {run_id}", file=sys.stderr)
    if session_id:
        print(f"{backend} session: {session_id}", file=sys.stderr)
    if detail:
        print(f"question/error: {redact(detail)}", file=sys.stderr)
    if session_id:
        # Without a session id there is nothing to resume; the operator handoff
        # still prints the remaining-budget command so the loop can continue.
        print(
            f"manual resume: {resume_command(backend, model, worktree, session_id, allow_agents)}",
            file=sys.stderr,
        )
    print(f"iterations remaining: {remaining}", file=sys.stderr)
    if remaining:
        print(
            "continue Ralph: "
            f"{restart_command(backend, model, worktree, prompt_path, remaining, timeout, allow_agents)}",
            file=sys.stderr,
        )
    else:
        print("No automatic replacement iteration remains.", file=sys.stderr)
    print("==========================================", end="", file=sys.stderr)
    print("\033[0m" if terminal else "", file=sys.stderr)


class CaffeinateAssertion:
    def __init__(self, worktree: Path) -> None:
        self.worktree = worktree
        self.process: subprocess.Popen[str] | None = None

    def __enter__(self) -> CaffeinateAssertion:
        try:
            self.process = subprocess.Popen(
                [caffeinate_executable(), "-im", "-w", str(os.getpid())],
                cwd=self.worktree,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except OSError as error:
            raise RalphError(f"could not start caffeinate: {error.strerror}") from None
        try:
            returncode = self.process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            return self
        raise RalphError(f"caffeinate exited during startup with status {returncode}")

    def ensure_alive(self) -> None:
        # The loop-wide assertion must cover the entire invocation. If it exits
        # unexpectedly (killed, crashed) the sleep guarantee is gone, so the loop
        # stops safely at the next boundary rather than continuing unprotected.
        if self.process is None:
            return
        code = self.process.poll()
        if code is not None:
            raise RalphError(
                f"the loop-wide caffeinate power assertion exited unexpectedly with status {code}"
            )

    def __exit__(self, *_: object) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()


def run_locked(
    args: argparse.Namespace,
    prompt_path: Path,
    prompt: str,
    worktree: Path,
    git_dir: Path,
    branch: str,
    status: str,
    slug: str,
) -> int:
    with CaffeinateAssertion(worktree) as assertion:
        return run_protected(
            args, prompt_path, prompt, worktree, git_dir, branch, status, slug, assertion
        )


def run_protected(
    args: argparse.Namespace,
    prompt_path: Path,
    prompt: str,
    worktree: Path,
    git_dir: Path,
    branch: str,
    status: str,
    slug: str,
    assertion: CaffeinateAssertion,
) -> int:
    if any(line and not line.startswith("##") for line in status.splitlines()):
        print("ralph: warning: worktree has uncommitted changes", file=sys.stderr)
    env = clean_environment(args.model, args.backend)
    # Redact subscription credentials from every readable and retained stream in
    # case backend output echoes an environment value.
    set_active_redactor(collect_secrets())
    print(
        "WARNING: Ralph always uses dangerous full-auto mode permissions; the backend may edit files "
        "and run commands without confirmation.",
        file=sys.stderr,
    )
    print(
        "WARNING: caffeinate cannot prevent lid-close or explicit sleep, power loss, or external "
        "network and service outages.",
        file=sys.stderr,
    )

    runs_root = secure_state_directory(git_dir, "ralph", "runs")
    run_dir = runs_root / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex[:8]
    )
    try:
        os.mkdir(run_dir)
    except FileExistsError:
        raise RalphError("Ralph run directory already exists") from None
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
            "timeout": args.timeout,
            "worktree": str(worktree),
        },
    )
    (run_dir / "git-status.txt").write_text(status, encoding="utf-8")
    started = datetime.now(timezone.utc).isoformat()
    iterations: list[dict[str, Any]] = []
    session_id: str | None = None
    outcome = "budget_exhausted"
    try:
        for number in range(1, args.iterations + 1):
            # The loop-wide sleep assertion must still be held before each fresh
            # session; a lost assertion stops the loop with retained evidence.
            assertion.ensure_alive()
            iteration_dir = (
                run_dir
                if number == 1
                else secure_state_directory(run_dir, f"iteration-{number:03d}")
            )
            # Mark the boundary between fresh sessions so multi-iteration
            # console output is attributable to a specific iteration.
            print(f"ralph: iteration {number} of {args.iterations}", file=sys.stderr)
            if args.backend == "claude":
                claude_preflight(worktree, slug, args.model, env, args.unsafe_allow_claude_agents)
            else:
                opencode_preflight(worktree, slug, args.model, env)
            iteration_started = datetime.now(timezone.utc).isoformat()
            outcome, session_id = execute_iteration(
                args.backend, worktree, iteration_dir, prompt, args.model, env, args.timeout
            )
            iterations.append(
                {
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "number": number,
                    "outcome": outcome,
                    "session_id": session_id,
                    "started_at": iteration_started,
                }
            )
            if outcome == "complete":
                break
    except HandoffError as error:
        iterations.append(
            {
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "number": number,
                "outcome": error.outcome,
                "reason": error.reason,
                "session_id": error.session_id,
                "started_at": iteration_started,
            }
        )
        outcome = error.outcome
        final_branch = record_final_git_state(worktree, run_dir, branch)
        write_json(
            run_dir / "outcome.json",
            {
                "final_branch": final_branch,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "iterations": iterations,
                "outcome": outcome,
                "session_id": error.session_id,
                "started_at": started,
            },
        )
        print_handoff(
            reason=error.reason,
            session_id=error.session_id,
            detail=error.detail,
            backend=args.backend,
            model=args.model,
            worktree=worktree,
            prompt_path=prompt_path,
            remaining=args.iterations - number,
            run_id=run_dir.name,
            timeout=args.timeout,
            allow_agents=args.unsafe_allow_claude_agents,
        )
        return 2
    except StartedIterationError as error:
        # A started iteration that stopped before any session metadata still
        # consumes its slot. There is no session to resume, but the operator
        # handoff must appear with the exact remaining-budget command.
        iterations.append(
            {
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "number": number,
                "outcome": error.outcome,
                "reason": error.reason,
                "session_id": None,
                "started_at": iteration_started,
            }
        )
        outcome = error.outcome
        final_branch = record_final_git_state(worktree, run_dir, branch)
        write_json(
            run_dir / "outcome.json",
            {
                "final_branch": final_branch,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "iterations": iterations,
                "outcome": outcome,
                "session_id": None,
                "started_at": started,
            },
        )
        print_handoff(
            reason=error.reason,
            session_id=None,
            detail=None,
            backend=args.backend,
            model=args.model,
            worktree=worktree,
            prompt_path=prompt_path,
            remaining=args.iterations - number,
            run_id=run_dir.name,
            timeout=args.timeout,
            allow_agents=args.unsafe_allow_claude_agents,
        )
        return 2
    except RalphError:
        final_branch = record_final_git_state(worktree, run_dir, branch)
        write_json(
            run_dir / "outcome.json",
            {
                "final_branch": final_branch,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "iterations": iterations,
                "outcome": "backend_failure",
                "started_at": started,
            },
        )
        raise
    final_branch = record_final_git_state(worktree, run_dir, branch)
    write_json(
        run_dir / "outcome.json",
        {
            "final_branch": final_branch,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "iterations": iterations,
            "outcome": outcome,
            "session_id": session_id,
            "started_at": started,
        },
    )
    if outcome == "complete":
        return 0
    print("RALPH INCOMPLETE: iteration budget exhausted without completion", file=sys.stderr)
    return 1


def validate_model(backend: str, model: str) -> None:
    if backend == "opencode" and (not model.startswith("openai/") or model == "openai/"):
        raise RalphError("model must use the openai/ provider")
    if backend == "claude" and not model.startswith("claude-"):
        raise RalphError("model must be a Claude subscription model")


def reject_backend_incompatible_flags(args: argparse.Namespace) -> None:
    # --unsafe-allow-claude-agents is meaningful only for the Claude backend: it
    # relaxes Claude-specific agent vectors and nothing about OpenCode. Reject it
    # fail-closed here — before any git, network, preflight, or backend work — so
    # it can never be threaded into an OpenCode preflight nor reproduced in a
    # generated OpenCode resume/run command, where it would be nonsensical.
    if getattr(args, "unsafe_allow_claude_agents", False) and args.backend != "claude":
        raise RalphError(
            "--unsafe-allow-claude-agents is only valid with --backend claude"
        )


def run(args: argparse.Namespace) -> int:
    reject_backend_incompatible_flags(args)
    if not 1 <= args.iterations <= 100:
        raise RalphError("iterations must be between 1 and 100")
    if not math.isfinite(args.timeout) or args.timeout < 0:
        raise RalphError("timeout must be zero or positive and finite")
    if args.timeout > MAX_ITERATION_TIMEOUT_SECONDS:
        raise RalphError(
            f"timeout must not exceed {MAX_ITERATION_TIMEOUT_SECONDS} seconds so backend "
            "request and Bash limits stay subordinate to Ralph's timer"
        )
    args.model = args.model or DEFAULT_MODELS[args.backend]
    validate_model(args.backend, args.model)

    prompt_path, prompt = read_prompt(args.prompt)
    worktree, git_dir, branch, status, slug = git_context(args.worktree)
    with WorktreeLock(git_dir, git_dir / "ralph" / "lock.json"):
        return run_locked(args, prompt_path, prompt, worktree, git_dir, branch, status, slug)


def clean(args: argparse.Namespace) -> int:
    requested = Path(args.worktree or os.getcwd()).expanduser().resolve()
    if not requested.is_dir():
        raise RalphError("worktree is not a directory")
    top = Path(command(["git", "rev-parse", "--show-toplevel"], cwd=requested).stdout.strip()).resolve()
    git_dir = Path(
        command(["git", "rev-parse", "--path-format=absolute", "--git-dir"], cwd=top).stdout.strip()
    ).resolve()
    state_root = git_dir / "ralph"
    # Refuse while a live loop holds the worktree lock so active logs and locks
    # cannot disappear underneath the process.
    lock = WorktreeLock(git_dir)
    lock.acquire()
    try:
        try:
            info = os.lstat(state_root)
        except FileNotFoundError:
            return 0
        # Never follow a symlink or delete an unexpected file type: only a real
        # Ralph state directory is removed, and shutil.rmtree does not follow
        # symlinked children, so backend transcripts and source files outside
        # .git/ralph are never touched.
        if stat.S_ISLNK(info.st_mode):
            raise RalphError("refusing to remove a symlinked Ralph state path")
        if not stat.S_ISDIR(info.st_mode):
            raise RalphError("Ralph state path is not a directory")
        shutil.rmtree(state_root)
    finally:
        lock.release()
    return 0


def resume(args: argparse.Namespace) -> int:
    reject_backend_incompatible_flags(args)
    validate_model(args.backend, args.model)
    worktree, _git_dir, _branch, _status, slug = git_context(args.worktree)
    # Re-establish the exact sanitized child environment and re-prove the
    # subscription trust boundary (OAuth, effective routing, model availability,
    # customization isolation) before any resumed model work. reject_unsafe_-
    # environment inside preflight fails closed on a newly added API credential
    # or custom endpoint, so recovery cannot silently inherit unsafe routing.
    env = clean_environment(args.model, args.backend)
    set_active_redactor(collect_secrets())
    if args.backend == "claude":
        claude_preflight(worktree, slug, args.model, env, args.unsafe_allow_claude_agents)
        backend_args = [
            "claude",
            "--resume",
            args.session,
            "--model",
            args.model,
            "--dangerously-skip-permissions",
            "--setting-sources",
            "project",
            "--strict-mcp-config",
            "--settings",
            CLAUDE_SETTINGS,
        ]
    else:
        opencode_preflight(worktree, slug, args.model, env)
        backend_args = [
            "opencode",
            "--pure",
            "--model",
            args.model,
            "--auto",
            "--session",
            args.session,
            "--dir",
            str(worktree),
        ]
    # caffeinate is launched by absolute path, exactly as automated iterations
    # do; preflight has already proved it exists. Holding the -im assertion for
    # the interactive session's whole lifetime replaces Ralph's own loop-level
    # assertion once control passes to the operator.
    argv = [caffeinate_executable(), "-im", *backend_args]
    print(
        "WARNING: Ralph is relaunching the backend session in dangerous full-auto mode; "
        "it may edit files and run commands without confirmation.",
        file=sys.stderr,
    )
    try:
        os.chdir(worktree)
        os.execvpe(argv[0], argv, env)
    except OSError as error:
        raise RalphError(f"could not launch {args.backend} for resume: {error.strerror}") from None
    return 2


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="ralph")
    subcommands = result.add_subparsers(dest="command", required=True)
    run_parser = subcommands.add_parser("run", help="run bounded coding-agent iterations")
    run_parser.add_argument("prompt")
    run_parser.add_argument("--backend", choices=["claude", "opencode"], required=True)
    run_parser.add_argument("--iterations", type=int, required=True)
    run_parser.add_argument("--model")
    run_parser.add_argument(
        "--timeout",
        type=float,
        default=2700,
        help=(
            "seconds allowed per iteration; zero disables the limit "
            f"(default: 2700, maximum: {MAX_ITERATION_TIMEOUT_SECONDS})"
        ),
    )
    run_parser.add_argument("--worktree")
    run_parser.add_argument(
        "--unsafe-allow-claude-agents",
        action="store_true",
        help=(
            "allow a repo's .claude/agents and settings.json 'agent' key instead of "
            "refusing them; Ralph then cannot prove Claude subagent isolation (unsafe)"
        ),
    )
    clean_parser = subcommands.add_parser("clean", help="remove Ralph state for a worktree")
    clean_parser.add_argument("--worktree")
    resume_parser = subcommands.add_parser(
        "resume", help="relaunch a handed-off session under Ralph's trust boundary"
    )
    resume_parser.add_argument("--backend", choices=["claude", "opencode"], required=True)
    resume_parser.add_argument("--model", required=True)
    resume_parser.add_argument("--session", required=True)
    resume_parser.add_argument("--worktree")
    resume_parser.add_argument(
        "--unsafe-allow-claude-agents",
        action="store_true",
        help=(
            "allow a repo's .claude/agents and settings.json 'agent' key instead of "
            "refusing them; Ralph then cannot prove Claude subagent isolation (unsafe)"
        ),
    )
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "run":
            return run(args)
        if args.command == "clean":
            return clean(args)
        if args.command == "resume":
            return resume(args)
    except RalphError as error:
        print(f"ralph: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
