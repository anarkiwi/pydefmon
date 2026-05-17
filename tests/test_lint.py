"""Lint checks. Currently: every tracked .py file must be black-clean.

We run ``black --check`` against the project's Python files and fail
the test if any file would be reformatted. The test is fast because
black short-circuits when files are already formatted.
"""

import os
import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Scope the lint check to project source -- ``preframr/`` is the
# importable package, ``tests/`` is the unit-test suite, and
# ``integration_tests/`` is the slower / docker-driven test suite.
# Root-level research scripts (study_*.py, breakdown_*.py, etc.) are
# research artifacts, not first-class project source, and aren't covered.
LINT_DIRS = ("preframr", "tests", "integration_tests")


def _python_files():
    """Yield project .py paths under LINT_DIRS, excluding generated dirs."""
    skip_dirs = {".git", "__pycache__", "build", "dist", ".venv", "venv"}
    for top in LINT_DIRS:
        for path in (REPO_ROOT / top).rglob("*.py"):
            if any(part in skip_dirs for part in path.relative_to(REPO_ROOT).parts):
                continue
            yield path


class TestBlackFormatting(unittest.TestCase):
    def test_all_python_files_are_black_clean(self):
        black = shutil.which("black")
        if black is None:
            self.skipTest("black not installed")
        files = sorted(_python_files())
        self.assertGreater(len(files), 0, "no .py files discovered")
        # ``--workers 1`` so the test doesn't fan out N CPU workers per
        # invocation. Three parallel docker builds each running tests
        # inside their RUN step would otherwise spawn 3*ncpu black
        # processes simultaneously and saturate the host.
        # Strip ``pytest-cov`` subprocess-injection env vars before
        # spawning black. Otherwise black inherits ``COV_CORE_SOURCE`` /
        # ``COV_CORE_CONFIG`` / ``COV_CORE_DATAFILE`` and pytest-cov's
        # sitecustomize hook tries to import pygments inside the black
        # subprocess. If pygments isn't installed in the runtime image,
        # the subprocess exits non-zero with a ModuleNotFoundError that
        # masquerades as a black-drift failure here.
        subprocess_env = {
            k: v for k, v in os.environ.items() if not k.startswith("COV_CORE_")
        }
        result = subprocess.run(
            [
                black,
                "--check",
                "--quiet",
                "--workers",
                "1",
                "--target-version",
                "py312",
                *[str(p) for p in files],
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env=subprocess_env,
        )
        if result.returncode != 0:
            # ``black --check`` prints offending paths to stderr.
            offenders = result.stderr.strip() or result.stdout.strip()
            self.fail("black --check failed; run `black .` to fix:\n" + offenders)


if __name__ == "__main__":
    unittest.main()
