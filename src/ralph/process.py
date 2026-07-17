"""Process-group control, timeouts, controlled-stop classification, and process
identity.

Invariants:
- A backend child is started in its own session (``start_new_session=True``) so it
  leads a process group. ``ProcessController`` captures the group id once, so it
  can signal the whole tree even after the leader exits and its pid is reaped. A
  group can outlive its leader while holding Ralph's pipes, so shutdown always
  escalates SIGINT -> SIGTERM -> SIGKILL across the *group*, never just the leader,
  and a spurious timer fire (leader gone, nothing else alive) is ignored.
- ``MAX_ITERATION_TIMEOUT_SECONDS`` is the largest iteration timeout Ralph accepts,
  kept far below ``BACKEND_TIMEOUT_MS`` expressed in seconds so the backend's own
  limits always stay subordinate to Ralph's timer.
- ``raise_if_controlled_stop`` is the single classifier turning a timed-out or
  interrupted controller into the right exception: a resumable ``HandoffError`` once
  a session exists, a consuming ``StartedIterationError`` before any session id
  arrived. Backends call it *before* blaming a contract failure so a stop Ralph
  itself caused is never misreported as backend misbehavior.

Depends on / must not know: ``errors``. It must not know which Backend it is
controlling beyond the ``backend`` label it echoes into stop reasons.

See also: ``launch`` (CaffeinateAssertion is a separate power assertion),
``backends.opencode`` / ``backends.claude`` (drive a controller per Iteration).
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from typing import Any

from .errors import HandoffError, StartedIterationError


# Largest iteration timeout Ralph accepts. Kept far below BACKEND_TIMEOUT_MS
# expressed in seconds (2147483.647) so the backend request/Bash limit always
# outlasts any accepted positive Ralph timeout by a wide margin.
MAX_ITERATION_TIMEOUT_SECONDS = 2_000_000
GRACEFUL_SHUTDOWN_SECONDS = 2
TERMINATE_SHUTDOWN_SECONDS = 1
# Brief pause between escalating a process group to SIGTERM and SIGKILL so a
# cooperating descendant can exit before it is force-killed.
GROUP_SETTLE_SECONDS = 0.05


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
