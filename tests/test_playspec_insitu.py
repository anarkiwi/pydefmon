"""Specs run inside whole-tune playback against the replay's own 6502.

A :class:`~pysidtracker.playspec.SpecPlayer` runs each modelled routine as its spec
and the replay's code for everything else; after every play call its registers and
the whole 64 KiB, outside the stack page, must equal a plain run of the replay.
"""

import pytest

from pysidtracker.testing import resolve_tune

from pydefmon import DefmonPlayer
from pydefmon._spec_layout import SIDROW_ENTRY, sidrow_binding

from tests._support import spec_player

TUNES = (
    "MUSICIANS/D/Demsky_Martin/Acid_Bassline.sid",
    "MUSICIANS/D/Dex-D/Short_Dream.sid",
    "MUSICIANS/I/Ilkke/Klotokok.sid",
)
FRAMES = 200
STACK = range(0x100, 0x200)


def _tune(relpath: str) -> bytes:
    path = resolve_tune(relpath)
    if path is None:
        pytest.skip(f"tune {relpath} unavailable (offline, not cached)")
    return path.read_bytes()


def _compare(data: bytes, spec_name: str, entries: dict) -> int:
    """Run both players ``FRAMES`` calls; the number of times the spec ran."""
    plain, hybrid = DefmonPlayer(data), spec_player(data, spec_name, entries)
    runs = [0]
    run = hybrid._run  # pylint: disable=protected-access

    def counting(entry):
        runs[0] += 1
        run(entry)

    hybrid._run = counting  # pylint: disable=protected-access
    for frame in range(FRAMES):
        assert plain.play_frame() == hybrid.play_frame(), frame
        # pylint: disable-next=protected-access
        ram, other = plain._mem, hybrid._mem
        differing = [
            hex(a) for a in range(0x10000) if a not in STACK and ram[a] != other[a]
        ]
        assert not differing, (frame, differing[:8])
    return runs[0]


@pytest.mark.parametrize("relpath", TUNES)
def test_sidrow_spec_inside_a_whole_tune(relpath):
    runs = _compare(_tune(relpath), "defmon_sidrow", {SIDROW_ENTRY: sidrow_binding})
    assert runs > 0
