"""Golden decode digests: every corpus replay re-expands to the same snapshot.

``tests/_support/replay_digests.txt`` holds ``<path> <layout> <sha1>`` of the
editor-layout snapshot each HVSC defMON replay decodes to, recorded before the
decoders were restructured; any refactor must keep them byte-identical.
"""

import hashlib
from pathlib import Path

import pytest

from pydefmon import DefmonSong
from tests._support import resolve_tune

_DIGESTS = Path(__file__).resolve().parent / "_support" / "replay_digests.txt"
_ROWS = [line.split() for line in _DIGESTS.read_text().splitlines() if line.strip()]


@pytest.mark.parametrize("relpath, layout, digest", _ROWS, ids=[r[0] for r in _ROWS])
def test_snapshot_digest(relpath, layout, digest):
    path = resolve_tune(relpath)
    if path is None:
        pytest.skip(f"{relpath} unreachable (offline)")
    song = DefmonSong.from_sid_bytes(path.read_bytes())
    assert song.replay_layout == layout
    assert hashlib.sha1(bytes(song.snapshot)).hexdigest() == digest
