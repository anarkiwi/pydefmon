"""Issue #38: the SID#2 (stereo) arrangers and the runtime-state page
fields (tempo, sub-frame count, stereo flag, SID#2 base) are documented
and modelled on ``DefmonSong``; ``optimize()`` consults the SID#2
arrangers when stereo is enabled, mirroring defMON's own packer."""

import unittest

from pydefmon import DIFF_REGIONS, DefmonSong, PatternEvent, STANDARD_SNAPSHOT_SIZE
from pydefmon.defmon import (
    CIA2_TIMER_HI,
    CIA2_TIMER_LO,
    LOAD_ADDRESS,
    SID2_BASE_HI,
    SID2_BASE_LO,
    STEREO_ENABLE,
    SUB_FRAME_COUNT,
)
from tests._support import fixture_path


def _off(addr: int) -> int:
    return addr - LOAD_ADDRESS


def _events(slot_a: int = 1):
    return (
        [PatternEvent.note_on(0x18, slot_a=slot_a, duration=0)]
        + [PatternEvent.delay(0) for _ in range(30)]
        + [PatternEvent.alt_end(duration=0)]
    )


def _duplicates() -> DefmonSong:
    """A song whose patterns 1 and 2 are byte-identical."""
    song = DefmonSong()
    song.set_pattern_events(1, _events())
    song.set_pattern_events(2, _events())
    return song


class TestStereoFieldAccessors(unittest.TestCase):
    """The six new fields read and write their documented snapshot bytes."""

    def test_arranger_views_map_to_their_pages(self):
        song = DefmonSong()
        for view, base in (
            (song.arranger_v4, 0x6E00),
            (song.arranger_v5, 0x6F00),
            (song.arranger_v6, 0x7000),
        ):
            self.assertEqual(len(view), 256)
            view[7] = 0x31
            self.assertEqual(song.snapshot[_off(base) + 7], 0x31)
            view[7] = 0
        # no aliasing between the six arranger pages
        song.arranger_v4[3] = 1
        song.arranger_v5[3] = 2
        song.arranger_v6[3] = 3
        song.arranger_v1[3] = 4
        song.arranger_v2[3] = 5
        song.arranger_v3[3] = 6
        self.assertEqual(
            (
                song.snapshot[_off(0x1B00) + 3],
                song.snapshot[_off(0x1C00) + 3],
                song.snapshot[_off(0x1D00) + 3],
                song.snapshot[_off(0x6E00) + 3],
                song.snapshot[_off(0x6F00) + 3],
                song.snapshot[_off(0x7000) + 3],
            ),
            (4, 5, 6, 1, 2, 3),
        )

    def test_cia2_timer_is_a_16_bit_word(self):
        song = DefmonSong()
        song.set_cia2_timer(0x0998)
        self.assertEqual(song.snapshot[CIA2_TIMER_LO], 0x98)
        self.assertEqual(song.snapshot[CIA2_TIMER_HI], 0x09)
        self.assertEqual(song.cia2_timer, 0x0998)
        song.set_cia2_timer(1)
        self.assertEqual(
            (song.snapshot[CIA2_TIMER_LO], song.snapshot[CIA2_TIMER_HI]), (1, 0)
        )

    def test_sub_frame_count_byte(self):
        song = DefmonSong()
        for value in (1, 2, 4, 8):
            song.set_sub_frame_count(value)
            self.assertEqual(song.snapshot[SUB_FRAME_COUNT], value)
            self.assertEqual(song.sub_frame_count, value)

    def test_stereo_flag_byte(self):
        song = DefmonSong()
        self.assertFalse(song.stereo_enable)
        song.set_stereo(True)
        self.assertEqual(song.snapshot[STEREO_ENABLE], 1)
        self.assertTrue(song.stereo_enable)
        song.set_stereo(False)
        self.assertEqual(song.snapshot[STEREO_ENABLE], 0)
        self.assertFalse(song.stereo_enable)

    def test_sid2_base_is_a_16_bit_word(self):
        song = DefmonSong()
        song.set_sid2_base(0xD520)
        self.assertEqual(song.snapshot[SID2_BASE_LO], 0x20)
        self.assertEqual(song.snapshot[SID2_BASE_HI], 0xD5)
        self.assertEqual(song.sid2_base, 0xD520)
        for word in (0xD400, 0xD540, 0xDE80, 0xDFE0):
            song.set_sid2_base(word)
            self.assertEqual(song.sid2_base, word)

    def test_setter_validation(self):
        song = DefmonSong()
        for bad in (0, -1, 0x10000):
            with self.assertRaises(ValueError):
                song.set_cia2_timer(bad)
        for bad in (0, 3, 5, 16, -1):
            with self.assertRaises(ValueError):
                song.set_sub_frame_count(bad)
        for bad in (0xD300, 0xD600, 0xD501, 0xD5F0, 0xF400, 0xD510):
            with self.assertRaises(ValueError):
                song.set_sid2_base(bad)
        with self.assertRaises(ValueError):
            song.set_step(1, v4=128)
        with self.assertRaises(ValueError):
            song.set_step(1, v5=-1)

    def test_set_step_sid2_is_opt_in(self):
        song = DefmonSong()
        song.arranger_v4[2] = 9
        song.arranger_v6[2] = 10
        song.set_step(2, v1=1, v2=2, v3=3)
        self.assertEqual(
            (song.arranger_v1[2], song.arranger_v2[2], song.arranger_v3[2]),
            (1, 2, 3),
        )
        self.assertEqual(
            (song.arranger_v4[2], song.arranger_v6[2]),
            (9, 10),
            "plain set_step must not clobber the SID#2 arrangers",
        )
        song.set_step(2, v4=5, v5=6, v6=7)
        self.assertEqual(
            (song.arranger_v4[2], song.arranger_v5[2], song.arranger_v6[2]), (5, 6, 7)
        )

    def test_set_jump_stereo_marks_both_chips(self):
        song = DefmonSong()
        song.set_jump(9, target=3, count=4)
        self.assertEqual(
            (song.arranger_v1[9], song.arranger_v2[9], song.arranger_v3[9]),
            (0xFF, 3, 4),
        )
        self.assertEqual(
            (song.arranger_v4[9], song.arranger_v5[9], song.arranger_v6[9]),
            (0, 0, 0),
        )
        song.set_stereo(True)
        song.set_jump(9, target=3, count=4, stereo=True)
        self.assertEqual(
            (song.arranger_v4[9], song.arranger_v5[9], song.arranger_v6[9]),
            (0xFF, 3, 4),
        )

    def test_clear_arranger_wipes_all_six_pages(self):
        song = DefmonSong()
        song.set_step(1, v1=1, v2=2, v3=3, v4=4, v5=5, v6=6)
        song.set_dl(1, 0x40)
        song.clear_arranger()
        for view in (
            song.arranger_v1,
            song.arranger_v2,
            song.arranger_v3,
            song.arranger_v4,
            song.arranger_v5,
            song.arranger_v6,
        ):
            self.assertTrue(all(b == 0 for b in view))
        self.assertEqual(song.sidtab_dl[1], 0x40, "clear_arranger keeps the DL column")
        song.clear_song_table()
        self.assertEqual(song.sidtab_dl[1], 0)

    def test_round_trip_preserves_every_field(self):
        song = DefmonSong()
        song.set_cia2_timer(0x4CC7)
        song.set_sub_frame_count(4)
        song.set_stereo(True)
        song.set_sid2_base(0xD540)
        song.set_step(0, v1=1, v2=2, v3=3, v4=4, v5=5, v6=6)
        song.set_jump(7, target=1, count=2, stereo=True)
        song.set_dl(3, 0x7F)
        reloaded = DefmonSong.from_bytes(song.to_bytes())
        self.assertEqual(reloaded.cia2_timer, 0x4CC7)
        self.assertEqual(reloaded.sub_frame_count, 4)
        self.assertTrue(reloaded.stereo_enable)
        self.assertEqual(reloaded.sid2_base, 0xD540)
        self.assertEqual(bytes(reloaded.arranger_v4), bytes(song.arranger_v4))
        self.assertEqual(bytes(reloaded.arranger_v5), bytes(song.arranger_v5))
        self.assertEqual(bytes(reloaded.arranger_v6), bytes(song.arranger_v6))
        self.assertEqual(bytes(reloaded.arranger_v1), bytes(song.arranger_v1))
        self.assertEqual(reloaded.sidtab_dl[3], 0x7F)


class TestDiffRegions(unittest.TestCase):
    """The sidtab region split off the SID#2 arrangers and the runtime-state page."""

    def test_split_regions_tilt_the_snapshot(self):
        self.assertEqual(
            sum(size for _n, _b, size in DIFF_REGIONS), STANDARD_SNAPSHOT_SIZE
        )
        tiles = sorted((base, base + size) for _n, base, size in DIFF_REGIONS)
        for i in range(len(tiles) - 1):
            self.assertEqual(tiles[i][1], tiles[i + 1][0])

    def test_sid2_and_runtime_state_are_named_regions(self):
        names = [name for name, _b, _s in DIFF_REGIONS]
        self.assertIn("arranger_v4", names)
        self.assertIn("arranger_v6", names)
        self.assertIn("runtime_state", names)
        song = DefmonSong()
        song.set_stereo(True)
        b = DefmonSong(bytes(song.snapshot))
        b.set_sid2_base(0xDF40)
        res = {r.name: r for r in song.diff(b)}
        self.assertTrue(res["runtime_state"].differing > 0)
        self.assertTrue(res["arranger_v4"].identical)
        song.arranger_v5[12] = 3
        res = {r.name: r for r in song.diff(b)}
        self.assertTrue(res["arranger_v5"].differing > 0)
        self.assertTrue(res["arranger_v4"].identical)


class TestOptimizeStereoAware(unittest.TestCase):
    """``optimize()`` rewrites SID#2 arranger references only when stereo
    is enabled and never touches a chip's jump-row target/count bytes."""

    def test_rewrites_sid2_refs_when_stereo_is_on(self):
        song = _duplicates()
        song.set_stereo(True)
        song.set_step(0, v4=2, v5=1, v6=2)
        song.optimize()
        self.assertEqual(
            (song.arranger_v4[0], song.arranger_v5[0], song.arranger_v6[0]), (1, 1, 1)
        )

    def test_leaves_sid2_refs_alone_when_stereo_is_off(self):
        song = _duplicates()
        song.set_step(0, v4=2, v5=1, v6=2)
        song.optimize()
        self.assertEqual(
            (song.arranger_v4[0], song.arranger_v5[0], song.arranger_v6[0]),
            (2, 1, 2),
            "defMON's packer gates the SID#2 rewrite on the stereo flag",
        )

    def test_skips_sid2_jump_rows(self):
        song = _duplicates()
        song.set_stereo(True)
        song.set_step(0, v4=2)
        song.set_jump(5, target=0, count=0, stereo=True)
        song.arranger_v5[5] = (
            2  # the jump target: must survive even though pat 2 is a remap victim
        )
        song.optimize()
        self.assertEqual(song.arranger_v4[5], 0xFF)
        self.assertEqual(song.arranger_v5[5], 2)
        self.assertEqual(song.arranger_v6[5], 0)
        self.assertEqual(song.arranger_v4[0], 1, "non-jump rows still get remapped")

    def test_skips_bit7_sid1_jump_rows_too(self):
        song = _duplicates()
        song.set_jump(8, target=1, count=6)
        song.arranger_v2[8] = 2  # target slot on the jump row: not a pattern reference
        song.optimize()
        self.assertEqual(song.arranger_v2[8], 2)

    def test_idempotent_with_stereo_fields(self):
        song = _duplicates()
        song.set_stereo(True)
        song.set_cia2_timer(0x4CC7)
        song.set_sub_frame_count(8)
        song.set_sid2_base(0xDE80)
        song.set_step(0, v4=2, v5=1)
        song.set_jump(4, target=0, count=1, stereo=True)
        once = bytes(song.optimize().snapshot)
        twice = bytes(song.optimize().snapshot)
        self.assertEqual(once, twice)
        optimized = DefmonSong(once)
        self.assertEqual(optimized.cia2_timer, 0x4CC7)
        self.assertEqual(optimized.sub_frame_count, 8)
        self.assertEqual(optimized.sid2_base, 0xDE80)


class TestFixturePinnedFields(unittest.TestCase):
    """The shipped defMON-release worktunes carry the exact field values
    the issue cites (skipped when the fixtures are unreachable)."""

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

    def _worktunes(self):
        out = []
        for name in self._WORKTUNES:
            try:
                out.append((name, DefmonSong.from_file(str(fixture_path(name)))))
            except FileNotFoundError:
                pass
        if not out:
            self.skipTest("defMON release worktunes unavailable (offline runner)")
        return dict(out)

    def test_dansa_in_tempo_fields(self):
        tunes = self._worktunes()
        song = tunes.get("dansa_in.prg")
        if song is None:
            self.skipTest("dansa_in.prg unavailable")
        self.assertEqual(song.cia2_timer, 0x4CC7)
        self.assertEqual(song.sub_frame_count, 1)
        self.assertFalse(song.stereo_enable)
        self.assertEqual(song.sid2_base, 0xDF00)
        for view in (song.arranger_v4, song.arranger_v5, song.arranger_v6):
            self.assertTrue(all(b == 0 for b in view))

    def test_automatas_sid2_arrangers(self):
        tunes = self._worktunes()
        song = tunes.get("automatas2017.prg")
        if song is None:
            self.skipTest("automatas2017.prg unavailable")
        self.assertEqual(bytes(song.arranger_v4[0:8]), bytes([1] * 8))
        self.assertEqual(song.arranger_v4[8], 0xFF)
        self.assertEqual(song.arranger_v4[9], 0)
        self.assertEqual(song.arranger_v5[0], 2)
        self.assertEqual(song.arranger_v5[4], 4)
        self.assertEqual(song.arranger_v6[0], 3)
        self.assertEqual(song.arranger_v6[4], 3)
        self.assertEqual(song.sub_frame_count, 8)
        self.assertEqual(song.cia2_timer, 0x0998)

    def test_field_sanity_across_all_worktunes(self):
        tunes = self._worktunes()
        for name, song in tunes.items():
            self.assertIn(song.sub_frame_count, (1, 2, 4, 8), name)
            self.assertGreaterEqual(song.cia2_timer, 1, name)
            self.assertFalse(song.stereo_enable, name)
            sid2 = sum(
                1
                for view in (song.arranger_v4, song.arranger_v5, song.arranger_v6)
                for b in view
                if b
            )
            if name != "automatas2017.prg":
                self.assertEqual(sid2, 0, name)

    def test_optimize_keeps_dead_sid2_data_byte_identical(self):
        """None of the shipped tunes enables stereo, so their SID#2
        arranger bytes are dead data that optimize() must not touch."""
        tunes = self._worktunes()
        song = tunes.get("automatas2017.prg")
        if song is None:
            self.skipTest("automatas2017.prg unavailable")
        before = (
            bytes(song.arranger_v4),
            bytes(song.arranger_v5),
            bytes(song.arranger_v6),
            song.cia2_timer,
            song.sub_frame_count,
            song.sid2_base,
        )
        song.optimize()
        self.assertEqual(
            (
                bytes(song.arranger_v4),
                bytes(song.arranger_v5),
                bytes(song.arranger_v6),
                song.cia2_timer,
                song.sub_frame_count,
                song.sid2_base,
            ),
            before,
        )


if __name__ == "__main__":
    unittest.main()
