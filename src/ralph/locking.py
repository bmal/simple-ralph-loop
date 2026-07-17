"""Git-private state directories, the worktree lock, and lock metadata.

Invariants:
- ``secure_state_directory`` walks each component beneath an already-resolved base
  with ``lstat``, refusing a symlink or non-directory anywhere in the chain, so
  Ralph state can never be redirected outside the worktree's private Git directory.
- The exclusive ``flock`` on the git-dir is the single source of truth for mutual
  exclusion. The ``lock.json`` metadata is advisory ownership info only: a missing,
  malformed, symlinked, or unparseable file carries no ownership claim, and
  ``read_lock_metadata`` refuses a non-regular file (which could redirect a write
  or leak a read).
- Recovery of a stale lock happens only while holding the flock, so no live
  process holds the lock: a stale, malformed, or reused-PID record is safe to
  overwrite. The one contradictory case — a recorded owner still alive with a
  matching process identity — means the flock guarantee was somehow bypassed, so
  it fails closed rather than clobber a possible live loop.

Depends on / must not know: ``errors``, ``process`` (``process_identity``), and
``gitcontext`` (``write_json``). It must not know what run state lives inside the
directories it secures.

See also: ``process`` (process identity for owner records), ``gitcontext``
(writes the metadata), ``cli`` (``clean`` acquires the lock), ``loop`` (holds it).
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import stat
from typing import Any

from .errors import RalphError
from .gitcontext import write_json
from .process import process_identity


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
