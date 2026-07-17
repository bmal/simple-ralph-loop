"""Packaging lifecycle: wheel/sdist build via the declared PEP 517 backend,
console entry point, metadata, and the no-third-party-dependency pins."""

from __future__ import annotations

from pathlib import Path
import email
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import ralph  # noqa: E402  (import after sys.path is extended)


class PackagingLifecycleTest(unittest.TestCase):
    """Non-skippable, backend-free packaging qualification.

    The supported qualification path is expected to provide the declared PEP 517
    build backend (setuptools). Its absence is a hard failure here rather than a
    silent skip, so a broken qualification environment cannot masquerade as a
    passing packaging check. No live language model is ever invoked.
    """

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import setuptools  # noqa: F401
        except ImportError as error:  # pragma: no cover - only on a broken env
            raise AssertionError(
                "the declared PEP 517 build backend (setuptools>=77) is required "
                "for packaging qualification and must not be skipped"
            ) from error

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        # Build from an isolated copy of the packaging inputs so wheel/sdist
        # construction never writes build artifacts into the checkout under test.
        self.source = self.work / "source"
        self.source.mkdir()
        shutil.copytree(ROOT / "src", self.source / "src")
        for name in ("pyproject.toml", "README.md", "LICENSE"):
            shutil.copy2(ROOT / name, self.source / name)

    def _pep517_build(self, hook: str) -> Path:
        """Invoke the declared setuptools PEP 517 backend directly, using only
        declared build tooling and the standard library (no third-party build
        front-end)."""
        out = self.work / hook
        out.mkdir()
        # The backend logs build chatter to stdout; redirect it to stderr so the
        # only thing on stdout is the returned artifact name.
        script = (
            "import contextlib, sys\n"
            "from setuptools import build_meta\n"
            "with contextlib.redirect_stdout(sys.stderr):\n"
            f"    name = build_meta.{hook}(sys.argv[1])\n"
            "sys.stdout.write(name)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(out)],
            cwd=self.source,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        artifact = out / result.stdout.strip()
        self.assertTrue(artifact.is_file(), f"{hook} produced no artifact: {result.stdout!r}")
        return artifact

    def test_wheel_install_exposes_cli_help_without_a_backend(self) -> None:
        wheel = self._pep517_build("build_wheel")
        target = self.work / "install"
        installed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                "--target",
                str(target),
                str(wheel),
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)

        executable = target / "bin" / "ralph"
        self.assertTrue(executable.is_file(), "console entry point was not installed")
        env = {**os.environ, "PYTHONPATH": str(target)}
        top = subprocess.run(
            [str(executable), "--help"], env=env, text=True, capture_output=True
        )
        self.assertEqual(top.returncode, 0, top.stderr)
        self.assertIn("{run,clean,resume}", top.stdout)
        for sub in ("run", "clean", "resume"):
            sub_help = subprocess.run(
                [str(executable), sub, "--help"], env=env, text=True, capture_output=True
            )
            self.assertEqual(sub_help.returncode, 0, sub_help.stderr)
            self.assertIn(sub, sub_help.stdout)

    def test_wheel_metadata_declares_entry_point_version_license_and_no_deps(self) -> None:
        wheel = self._pep517_build("build_wheel")
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            metadata_text = archive.read(
                next(n for n in names if n.endswith(".dist-info/METADATA"))
            ).decode("utf-8")
            entry_points = archive.read(
                next(n for n in names if n.endswith(".dist-info/entry_points.txt"))
            ).decode("utf-8")
            license_members = [
                n
                for n in names
                if ".dist-info/licenses/" in n or n.endswith(".dist-info/LICENSE")
            ]
            license_text = archive.read(license_members[0]).decode("utf-8") if license_members else ""

        metadata = email.message_from_string(metadata_text)
        self.assertEqual(metadata["Name"], "simple-ralph-loop")
        self.assertEqual(metadata["Version"], "0.1.0")
        self.assertEqual(metadata["Version"], ralph.__version__)
        self.assertIn("3.11", metadata["Requires-Python"] or "")
        # Empty runtime dependency set: setuptools omits Requires-Dist entirely.
        self.assertIsNone(metadata.get_all("Requires-Dist"))
        # License attribution: SPDX expression plus the bundled license file.
        self.assertEqual(metadata["License-Expression"], "MIT")
        self.assertTrue(license_members, "LICENSE file was not bundled in the wheel")
        self.assertIn("MIT License", license_text)
        # Console entry point routes `ralph` to the CLI main.
        self.assertIn("[console_scripts]", entry_points)
        self.assertIn("ralph = ralph.cli:main", entry_points)
        # The corrected README must not resurface the unpublished-PyPI claim.
        self.assertNotIn("pipx install simple-ralph-loop", metadata_text)

    def test_sdist_contains_sources_and_metadata(self) -> None:
        sdist = self._pep517_build("build_sdist")
        self.assertTrue(sdist.name.endswith(".tar.gz"), sdist.name)
        with tarfile.open(sdist, "r:gz") as archive:
            members = archive.getnames()
            pkg_info_name = next(n for n in members if n.endswith("/PKG-INFO"))
            pkg_info = archive.extractfile(pkg_info_name).read().decode("utf-8")

        relative = {name.split("/", 1)[1] for name in members if "/" in name}
        for expected in (
            "pyproject.toml",
            "README.md",
            "LICENSE",
            "src/ralph/__init__.py",
            "src/ralph/cli.py",
            "PKG-INFO",
        ):
            self.assertIn(expected, relative, f"{expected} missing from sdist")
        metadata = email.message_from_string(pkg_info)
        self.assertEqual(metadata["Name"], "simple-ralph-loop")
        self.assertEqual(metadata["Version"], "0.1.0")
        self.assertEqual(metadata["License-Expression"], "MIT")
        self.assertIsNone(metadata.get_all("Requires-Dist"))
