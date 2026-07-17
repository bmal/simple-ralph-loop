"""Worktree locking and clean: git-private state directories, the live/stale
worktree lock and its recovery, and safe clean of only selected state."""

from __future__ import annotations

from pathlib import Path
import json
import os
import subprocess
import tempfile
import unittest

from harness import RalphCliTestCase
from ralph import errors, locking, process


class WorktreeLockingTest(RalphCliTestCase):
    def test_live_lock_refuses_a_second_loop_and_dead_owner_is_recovered(self) -> None:
        blocker = self.base / "blocked"
        blocker.touch()
        first_calls = self.base / "first-calls"
        first_calls.mkdir()
        first = subprocess.Popen(
            self._command(),
            cwd=self.base,
            env=self._environment({"FAKE_BLOCK_FILE": str(blocker), "FAKE_CALLS": str(first_calls)}),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: first.poll() is None and first.kill())
        self._await_ready(Path(f"{blocker}.ready"), first, what="first loop")

        second = self.run_ralph()
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("already running", second.stderr)
        cleaning = self.clean_ralph()
        self.assertNotEqual(cleaning.returncode, 0)
        self.assertIn("already running", cleaning.stderr)

        first.kill()
        first.communicate(timeout=5)
        blocker.unlink()
        recovered = self.run_ralph()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)

    def test_clean_removes_only_selected_repository_ralph_state(self) -> None:
        result = self.run_ralph()
        self.assertEqual(result.returncode, 0, result.stderr)
        source = self.repo / "keep.txt"
        source.write_text("source", encoding="utf-8")
        backend_state = self.base / "opencode-session"
        backend_state.write_text("transcript", encoding="utf-8")

        cleaned = self.clean_ralph()

        self.assertEqual(cleaned.returncode, 0, cleaned.stderr)
        self.assertFalse((self.repo / ".git" / "ralph").exists())
        self.assertEqual(list((self.repo / ".git").glob("ralph*")), [])
        self.assertEqual(source.read_text(), "source")
        self.assertEqual(backend_state.read_text(), "transcript")

    def test_linked_worktrees_have_independent_locks(self) -> None:
        tracked = self.repo / "tracked.txt"
        tracked.write_text("initial", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.repo, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Ralph Test",
                "-c",
                "user.email=ralph@example.invalid",
                "commit",
                "-m",
                "initial",
            ],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        other = self.base / "other-worktree"
        subprocess.run(
            ["git", "worktree", "add", "-b", "other", str(other)],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        blocker = self.base / "worktree-blocked"
        blocker.touch()
        first_calls = self.base / "worktree-first-calls"
        first_calls.mkdir()
        first = subprocess.Popen(
            self._command(),
            cwd=self.base,
            env=self._environment({"FAKE_BLOCK_FILE": str(blocker), "FAKE_CALLS": str(first_calls)}),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: first.poll() is None and first.kill())
        self._await_ready(Path(f"{blocker}.ready"), first, what="first worktree")

        independent = subprocess.run(
            self._command(worktree=other),
            cwd=self.base,
            env=self._environment(),
            text=True,
            capture_output=True,
        )

        self.assertEqual(independent.returncode, 0, independent.stderr)
        blocker.unlink()
        first.communicate(timeout=5)

    def test_linked_worktrees_keep_independent_state(self) -> None:
        other = self._add_linked_worktree("second")
        main_run = self.run_ralph()
        self.assertEqual(main_run.returncode, 0, main_run.stderr)
        for path in self.calls.iterdir():
            path.unlink()
        other_run = subprocess.run(
            self._command(worktree=other),
            cwd=self.base,
            env=self._environment(),
            text=True,
            capture_output=True,
        )
        self.assertEqual(other_run.returncode, 0, other_run.stderr)

        main_runs = self.repo / ".git" / "ralph" / "runs"
        linked_runs = self.repo / ".git" / "worktrees" / "second" / "ralph" / "runs"
        self.assertTrue(any(main_runs.iterdir()))
        self.assertTrue(linked_runs.is_dir() and any(linked_runs.iterdir()))

    def test_run_refuses_symlinked_ralph_state_directory(self) -> None:
        outside = self.base / "outside-state"
        outside.mkdir()
        os.symlink(outside, self.repo / ".git" / "ralph")

        result = self.run_ralph()

        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("symlink", result.stderr)
        # Nothing was redirected outside the resolved Git directory.
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.calls / "opencode").exists())

    def test_run_refuses_non_directory_ralph_state(self) -> None:
        (self.repo / ".git" / "ralph").write_text("not a directory", encoding="utf-8")

        result = self.run_ralph()

        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("not a directory", result.stderr)

    def test_run_refuses_symlinked_runs_subdirectory(self) -> None:
        (self.repo / ".git" / "ralph").mkdir()
        outside = self.base / "outside-runs"
        outside.mkdir()
        os.symlink(outside, self.repo / ".git" / "ralph" / "runs")

        result = self.run_ralph()

        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("symlink", result.stderr)
        self.assertEqual(list(outside.iterdir()), [])

    def test_clean_refuses_symlinked_state_and_preserves_target(self) -> None:
        outside = self.base / "outside-clean"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep", encoding="utf-8")
        os.symlink(outside, self.repo / ".git" / "ralph")

        result = self.clean_ralph()

        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("symlink", result.stderr)
        self.assertTrue((outside / "keep.txt").exists())

    def test_clean_removes_state_without_following_symlinked_children(self) -> None:
        self.assertEqual(self.run_ralph().returncode, 0)
        outside = self.base / "outside-child"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep", encoding="utf-8")
        os.symlink(outside, self.repo / ".git" / "ralph" / "link-to-outside")

        cleaned = self.clean_ralph()

        self.assertEqual(cleaned.returncode, 0, cleaned.stderr)
        self.assertFalse((self.repo / ".git" / "ralph").exists())
        # The symlink target and its contents were never followed or deleted.
        self.assertTrue((outside / "keep.txt").exists())


class WorktreeLockMetadataTest(unittest.TestCase):
    """Deterministic ownership-verification coverage for stale lock recovery."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.git_dir = self.base / "gitdir"
        self.git_dir.mkdir()
        self.meta = self.git_dir / "ralph" / "lock.json"

    def _write_meta(self, value: object) -> None:
        self.meta.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, str):
            self.meta.write_text(value, encoding="utf-8")
        else:
            self.meta.write_text(json.dumps(value), encoding="utf-8")

    def test_absent_metadata_acquires_and_records_owner(self) -> None:
        lock = locking.WorktreeLock(self.git_dir, self.meta)
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertTrue(lock.acquired)
        self.assertEqual(json.loads(self.meta.read_text())["pid"], os.getpid())

    def test_malformed_metadata_is_treated_as_stale_and_recovered(self) -> None:
        self._write_meta("{ not valid json")
        lock = locking.WorktreeLock(self.git_dir, self.meta)
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertTrue(lock.acquired)
        self.assertEqual(json.loads(self.meta.read_text())["pid"], os.getpid())

    def test_reused_pid_with_mismatched_identity_is_recovered(self) -> None:
        # The recorded PID is live (our own) but its identity does not match, as
        # happens when the OS reuses a dead owner's PID for an unrelated process.
        self._write_meta({"pid": os.getpid(), "identity": "not-the-real-identity"})
        lock = locking.WorktreeLock(self.git_dir, self.meta)
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertTrue(lock.acquired)

    def test_inconsistent_pid_type_is_recovered(self) -> None:
        self._write_meta({"pid": "not-an-int", "identity": "whatever"})
        lock = locking.WorktreeLock(self.git_dir, self.meta)
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertTrue(lock.acquired)

    def test_live_matching_owner_refuses_recovery(self) -> None:
        self._write_meta({"pid": os.getpid(), "identity": process.process_identity(os.getpid())})
        lock = locking.WorktreeLock(self.git_dir, self.meta)
        with self.assertRaises(errors.RalphError) as caught:
            lock.acquire()
        self.assertIn("live matching owner", str(caught.exception))
        self.assertFalse(lock.acquired)

    def test_symlinked_metadata_file_is_refused(self) -> None:
        self.meta.parent.mkdir(parents=True)
        target = self.base / "outside.json"
        target.write_text("{}", encoding="utf-8")
        os.symlink(target, self.meta)
        lock = locking.WorktreeLock(self.git_dir, self.meta)
        with self.assertRaises(errors.RalphError) as caught:
            lock.acquire()
        self.assertIn("not a regular file", str(caught.exception))
        self.assertFalse(lock.acquired)

    def test_symlinked_state_root_is_refused(self) -> None:
        outside = self.base / "outside-root"
        outside.mkdir()
        os.symlink(outside, self.git_dir / "ralph")
        lock = locking.WorktreeLock(self.git_dir, self.meta)
        with self.assertRaises(errors.RalphError) as caught:
            lock.acquire()
        self.assertIn("symlink", str(caught.exception))
        self.assertFalse(lock.acquired)

    def test_release_after_refused_recovery_leaves_lock_free(self) -> None:
        self._write_meta({"pid": os.getpid(), "identity": process.process_identity(os.getpid())})
        first = locking.WorktreeLock(self.git_dir, self.meta)
        with self.assertRaises(errors.RalphError):
            first.acquire()
        # The exclusive flock was released, so a clean record can be recovered.
        self.meta.unlink()
        second = locking.WorktreeLock(self.git_dir, self.meta)
        second.acquire()
        self.addCleanup(second.release)
        self.assertTrue(second.acquired)
