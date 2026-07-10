"""Register-log surface (``pydefmon.reglog``): the shared py* convention.

Mirrors ``pygoattracker`` / ``pymusicassembler`` register logs so the
same downstream tooling (including deplayroutine's generic-interpreter
validator harness) can consume a defMON tune's per-frame output.

Fixtures are not redistributed; tests ``skipTest`` when the cached
defMON ``.prg`` corpus is absent (``python -m tools.fetch_fixtures``).
"""

from __future__ import annotations

import io
import os
import unittest
from pathlib import Path

from pysidtracker import SidParseError

from pydefmon import (
    DefmonSong,
    RegWrite,
    iter_register_writes,
    read_reglog,
    write_reglog,
)
from pydefmon.defmon_player import DefmonPlayer, SID_REG_BASE
from pydefmon.reglog import SID_REGISTERS

from tests._support import FIXTURES_DIR

GLOW_WORM = Path(FIXTURES_DIR) / "glow_worm.prg"


def _song():
    if not GLOW_WORM.exists():
        raise unittest.SkipTest(
            f"fixture {GLOW_WORM} absent; run python -m tools.fetch_fixtures"
        )
    return DefmonSong.from_file(str(GLOW_WORM))


class TestRegLog(unittest.TestCase):
    def test_reg_offsets_and_values_match_play_frame(self):
        """Each yielded write is ``play_frame``'s write rebased to a
        ``0..$18`` register offset, in the same per-frame order."""
        song = _song()
        writes = list(iter_register_writes(song, max_frames=3))
        # Re-run the player directly and rebase to offsets for comparison.
        player = DefmonPlayer(_song())
        expected = []
        for _ in range(3):
            for reg, val in player.play_frame():
                expected.append((reg - SID_REG_BASE, val))
        self.assertEqual([(w.reg, w.val) for w in writes], expected)
        # Every register offset is in the SID file range.
        self.assertTrue(all(0 <= w.reg <= 0x18 for w in writes))

    def test_clock_layout_default_per_tune_rate(self):
        """Default cadence is the song's own per-tune player-IRQ rate;
        writes within a frame are ``write_spacing`` apart."""
        song = _song()
        cpf = DefmonPlayer(_song()).cycles_per_frame
        writes = list(iter_register_writes(song, max_frames=2))
        n0 = len(DefmonPlayer(_song()).play_frame())
        # Frame 0 writes start at clock 0, spaced 16.
        self.assertEqual(writes[0].clock, 0)
        self.assertEqual(writes[1].clock, 16)
        # Frame 1 starts one per-tune frame later.
        self.assertEqual(writes[n0].clock, cpf)

    def test_clock_options_override_cadence(self):
        song = _song()
        n0 = len(DefmonPlayer(_song()).play_frame())
        writes = list(
            iter_register_writes(
                song, max_frames=2, cycles_per_frame=1000, write_spacing=2
            )
        )
        self.assertEqual(writes[1].clock, 2)
        self.assertEqual(writes[n0].clock, 1000)

    def test_round_trip_text(self):
        song = _song()
        writes = list(iter_register_writes(song, max_frames=10))
        buf = io.StringIO()
        write_reglog(writes, buf)
        buf.seek(0)
        self.assertEqual(read_reglog(buf), writes)

    def test_round_trip_path(self):
        import tempfile

        song = _song()
        writes = list(iter_register_writes(song, max_frames=5))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "log.txt")
            write_reglog(writes, path)
            self.assertEqual(read_reglog(path), writes)

    def test_bad_line_raises(self):
        with self.assertRaises(SidParseError):
            read_reglog(io.StringIO("1 2\n"))

    def test_regwrite_is_named_triple(self):
        w = RegWrite(123, 4, 5)
        self.assertEqual((w.clock, w.reg, w.val), (123, 4, 5))
        self.assertEqual(SID_REGISTERS, 0x19)


if __name__ == "__main__":
    unittest.main()
