"""pydefmon's .prg worktune model checked against defMON's own LOAD (issue #40).

Runs defMON's ``$CEB2`` decode on the real decrunched editor as the oracle and
asserts ``unpacked_snapshot()`` reproduces its post-LOAD image over the codec's
write range ``$1800..``top``; also pins the to_bytes/from_bytes round trip.
"""

import unittest

from pysidtracker.emu import EmulatorUnavailable

from pydefmon import DefmonSong
from pydefmon._worktune_oracle import load_worktune
from tests._support import editor_image_bytes, fixture_path

_WORKTUNES = (
    "automatas2017.prg",
    "dansa_in.prg",
    "f7sus4_ngc1277.prg",
    "glow_worm.prg",
    "hive_machines.prg",
    "matsam0t.prg",
    "mondaynight2017.prg",
    "radio_robon95.prg",
    "vacuole.prg",
)


def _dest_top(body: bytes) -> int:
    dest_lo = body[-2]
    dest_hi = body[-1] + 0x18
    return dest_lo | (dest_hi << 8)


class TestNativeWorktunes(unittest.TestCase):
    """The nine shipped worktunes, decoded by defMON itself as the oracle."""

    @classmethod
    def setUpClass(cls):
        cls.editor = editor_image_bytes()

    def setUp(self):
        if self.editor is None:
            self.skipTest("defMON editor image unavailable (offline runner)")

    def _worktunes(self):
        names = []
        for n in _WORKTUNES:
            try:
                fixture_path(n)
                names.append(n)
            except FileNotFoundError:
                pass
        if not names:
            self.skipTest("no worktune fixtures available (offline runner)")
        return names

    def test_unpacked_matches_defmon_load(self):
        """``unpacked_snapshot()`` equals defMON's own ``$CEB2`` LOAD result.

        Compared over ``$1800..``top``: two of the nine tunes encode a shorter
        run (``top``=$7163), and the bytes at/above it retain the editor's prior
        RAM rather than authored data, so they are outside the codec's write range.
        """
        for name in self._worktunes():
            raw = fixture_path(name).read_bytes()
            try:
                ref = load_worktune(self.editor, raw)
            except EmulatorUnavailable:
                self.skipTest("6502 backend unavailable")
            got = bytes(DefmonSong.from_bytes(raw).unpacked_snapshot())
            n = _dest_top(raw[2:]) - 0x1800 + 1
            self.assertEqual(
                got[:n],
                ref[:n],
                f"{name}: unpacked_snapshot differs from defMON's own LOAD over "
                f"$1800..${_dest_top(raw[2:]) - 1:04X}",
            )

    def test_round_trip_preserves_post_load_model(self):
        """``to_bytes`` -> ``from_bytes`` -> ``unpacked_snapshot`` is stable."""
        for name in self._worktunes():
            raw = fixture_path(name).read_bytes()
            song = DefmonSong.from_bytes(raw)
            before = bytes(song.unpacked_snapshot())
            after = bytes(DefmonSong.from_bytes(song.to_bytes()).unpacked_snapshot())
            self.assertEqual(
                before, after, f"{name}: round trip changed the post-LOAD model"
            )


if __name__ == "__main__":
    unittest.main()
