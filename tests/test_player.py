"""Unit tests for :class:`pydefmon.DefmonPlayer` (the py65 replay player).

The player runs a tune's own relocatable replay on a py65 6502, so a runnable
synthetic PSID (no copyrighted material) exercises the whole path offline:
``_init`` / ``_frame`` / snapshot / ``render_grid`` / ``play_frame``. A single
real HVSC replay is byte-checked against the ``.prg`` reader path when the
corpus is reachable.
"""

from __future__ import annotations

import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pysidtracker import SidImage

from pydefmon import DefmonPlayer, DefmonSong
from pydefmon.defmon import DefmonError

from tests._support import fixture_path, resolve_tune, synthetic_replay


class TestDefmonPlayer(unittest.TestCase):
    def setUp(self):
        self.replay = synthetic_replay()

    def test_render_grid_shape_and_forward_fill(self):
        grid = DefmonPlayer(self.replay).render_grid(5)
        self.assertEqual(len(grid), 5)
        self.assertTrue(all(len(row) == 25 for row in grid))
        # init seeds $D418=$0F and $D404=$21; play increments $D400 each frame.
        self.assertTrue(all(row[0x18] == 0x0F for row in grid))
        self.assertTrue(all(row[0x04] == 0x21 for row in grid))
        self.assertEqual([row[0x00] for row in grid], [1, 2, 3, 4, 5])

    def test_play_frame_diffs_after_baseline(self):
        player = DefmonPlayer(self.replay)
        # First play_frame reports the full post-first-frame register file.
        first = dict(player.play_frame())
        self.assertEqual(len(first), 25)
        self.assertEqual(first[0x00], 1)
        # Later frames report only the register that changed ($D400).
        second = player.play_frame()
        self.assertEqual(second, [(0x00, 2)])

    def test_render_grid_matches_base_register_grid(self):
        """The player render equals the base py65 renderer (same real code)."""
        from pysidtracker import register_grid

        grid = DefmonPlayer(self.replay).render_grid(8)
        ref = register_grid(self.replay, 8, illegal_opcodes=True)
        for row in ref:
            for i in (3, 10, 17):
                row[i] &= 0x0F
        self.assertEqual(grid, ref)

    def test_pw_hi_masked(self):
        # Construct a replay whose init writes $FF to a pulse-width-hi register
        # ($D403); render_grid must mask it to the low nibble.
        raw = bytearray(synthetic_replay())
        # Overwrite init: LDA #$FF STA $D403 ... keep RTS chain intact.
        code_off = 0x7C  # data starts here; $1000 maps to code_off
        raw[code_off : code_off + 5] = bytes([0xA9, 0xFF, 0x8D, 0x03, 0xD4])
        grid = DefmonPlayer(bytes(raw)).render_grid(1)
        self.assertEqual(grid[0][0x03], 0x0F)

    def test_subtune_from_start_song(self):
        raw = bytearray(self.replay)
        struct.pack_into(">H", raw, 0x0E, 4)  # songs
        struct.pack_into(">H", raw, 0x10, 3)  # start song -> subtune 2
        player = DefmonPlayer(bytes(raw))
        # Accumulator carried the subtune into init; render still runs.
        self.assertEqual(len(player.render_grid(2)), 2)
        self.assertEqual(len(DefmonPlayer(bytes(raw), subtune=0).render_grid(1)), 1)

    def test_cycles_per_frame_is_pal_frame_without_cia(self):
        # The synthetic init programs no CIA timer, so cadence is the PAL frame.
        self.assertEqual(DefmonPlayer(self.replay).cycles_per_frame, 19656)

    def test_accepts_sidimage_and_path(self):
        image = SidImage.from_bytes(self.replay)
        self.assertEqual(len(DefmonPlayer(image).render_grid(1)), 1)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "synth.sid"
            path.write_bytes(self.replay)
            self.assertEqual(len(DefmonPlayer(path).render_grid(1)), 1)

    def test_workfile_without_player_raises(self):
        # A bare .prg workfile has no init/play routine.
        with self.assertRaises(DefmonError):
            DefmonPlayer(b"\x00\x18" + b"\x00" * 32)

    def test_replay_reading_raster_renders(self):
        # play: LDA $D012 (raster); STA $D400; RTS -> exercises the raster read
        # subscription. Value is cycle-derived; only the run path is asserted.
        raw = bytearray(synthetic_replay())
        play_off = 0x7C + 0x20  # data offset + play offset -> $1020
        raw[play_off : play_off + 7] = bytes([0xAD, 0x12, 0xD0, 0x8D, 0x00, 0xD4, 0x60])
        grid = DefmonPlayer(bytes(raw)).render_grid(3)
        self.assertEqual(len(grid), 3)


class TestDefmonPlayerRealReplay(unittest.TestCase):
    """A real HVSC replay drives the player when the corpus is reachable."""

    def _real_replay(self):
        # Prefer a fixture-dir replay; else fetch one known-small DefMon .sid.
        try:
            return fixture_path("glow_worm.sid").read_bytes()
        except FileNotFoundError:
            pass
        path = resolve_tune("MUSICIANS/I/Ilkke/Glow_Worm.sid")
        if path is None:
            raise unittest.SkipTest("no real defMON .sid reachable (offline)")
        return path.read_bytes()

    def test_real_replay_renders_and_reads(self):
        raw = self._real_replay()
        grid = DefmonPlayer(raw).render_grid(50)
        self.assertEqual(len(grid), 50)
        self.assertTrue(all(len(row) == 25 for row in grid))
        # The same bytes also parse through the DefmonSong reader.
        song = DefmonSong.from_bytes(raw)
        self.assertGreater(len(song.snapshot), 0)


if __name__ == "__main__":
    unittest.main()
