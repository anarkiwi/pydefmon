"""Lint checks on the repo: every tracked .py file under the package + tests
must be ``black --check`` clean, and every ``tests/...py`` path cited in the
docs must exist."""

import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LINT_DIRS = ("pydefmon", "scripts", "tests")

DOC_FILES = (REPO_ROOT / "README.md", *(REPO_ROOT / "docs").glob("*.md"))
TEST_REF_RE = re.compile(r"(?<![\w.-])(tests/[\w-]+(?:/[\w-]+)*\.py)")


def _python_files():
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
                "py311",
                *[str(p) for p in files],
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env=subprocess_env,
        )
        if result.returncode != 0:
            offenders = result.stderr.strip() or result.stdout.strip()
            self.fail("black --check failed; run `black .` to fix:\n" + offenders)


class TestDocsTestReferences(unittest.TestCase):
    """Every ``tests/...py`` path cited in the docs must exist in the repo."""

    def test_cited_test_files_exist(self):
        self.assertGreater(len(DOC_FILES), 1, "no doc files found to lint against")
        missing = []
        for doc in sorted(DOC_FILES):
            cited = sorted(set(TEST_REF_RE.findall(doc.read_text(encoding="utf-8"))))
            for ref in cited:
                if not (REPO_ROOT / ref).is_file():
                    missing.append(f"{doc.relative_to(REPO_ROOT)}: {ref}")
        self.assertEqual(
            missing,
            [],
            "docs cite test files that do not exist:\n" + "\n".join(missing),
        )


if __name__ == "__main__":
    unittest.main()
