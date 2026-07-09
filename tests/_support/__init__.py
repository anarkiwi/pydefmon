"""Test-only helpers. Not part of the public package."""

import os
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES_DIR = Path(
    os.environ.get("PYDEFMON_FIXTURES_DIR", REPO_ROOT / "build" / "fixtures")
)


def hvsc_root() -> Optional[Path]:
    """Return the local HVSC ``C64Music`` tree, or ``None`` if unavailable.

    Honours ``$HVSC`` (or ``$PYDEFMON_HVSC``). HVSC tunes are copyright
    works and are never redistributed with pydefmon; corpus tests that need
    them ``skipTest`` when this returns ``None``.
    """
    for env in ("HVSC", "PYDEFMON_HVSC"):
        val = os.environ.get(env)
        if val and Path(val).is_dir():
            return Path(val)
    return None


def find_defmon_sids(root: Path) -> List[Path]:
    """Return every DefMon ``.sid`` replay under ``root`` (by player signature).

    Mirrors what sidid identifies as ``DefMon``: it scans for defMON's player
    SID-write-band signature in each PSID/RSID image.
    """
    from pydefmon._sid_format import SIGNATURE

    anchor = bytes(SIGNATURE[4:10])  # fixed (non-wildcard) run for prefilter

    def has_sig(buf: bytes) -> bool:
        start = 0
        while True:
            j = buf.find(anchor, start)
            if j < 0:
                return False
            a = j - 4
            if a >= 0 and all(
                w is None or (a + i < len(buf) and buf[a + i] == w)
                for i, w in enumerate(SIGNATURE)
            ):
                return True
            start = j + 1

    out: List[Path] = []
    for path in root.rglob("*.sid"):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if data[:4] in (b"PSID", b"RSID") and has_sig(data):
            out.append(path)
    return sorted(out)


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
