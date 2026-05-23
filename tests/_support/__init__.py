"""Test-only helpers. Not part of the public package."""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES_DIR = Path(
    os.environ.get("PYDEFMON_FIXTURES_DIR", REPO_ROOT / "build" / "fixtures")
)


def fixture_path(name: str) -> Path:
    """Return the path to a fixture file, or raise FileNotFoundError.

    Tests should call this and ``skipTest`` on miss — fixtures are not
    redistributed (run ``python -m tools.fetch_fixtures`` to populate).
    """
    p = FIXTURES_DIR / name
    if not p.exists():
        raise FileNotFoundError(
            f"missing fixture {name!r} under {FIXTURES_DIR}; "
            "run `python -m tools.fetch_fixtures` to populate"
        )
    return p
