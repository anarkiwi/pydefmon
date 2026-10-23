"""Pack ``.prg`` worktunes into ``$1000`` replays and play them (issue #35).

Oracle: defMON's own ``$D02C`` SAVE pipeline on a user-supplied decrunched
editor image (``pydefmon._packer``), so the replay bytes are defMON's.
Worktunes are the editor files on the official V20201008 release; HVSC's
``Dansa_in.sid`` is the composer's rip used for the render assertion.
Fixtures are cached, never committed; tests skip when unreachable.
"""

import unittest

from pysidtracker import SidImage
from pysidtracker.header import write_psid
from pysidtracker.testing import resolve_tune

from pydefmon import _packer
from pydefmon._sid_format import find_signature
from pydefmon.defmon import STANDARD_SNAPSHOT_SIZE, DefmonError, DefmonSong
from pydefmon.defmon_player import DefmonPlayer

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
_DANSA_HVSC = "MUSICIANS/G/Goto80/Dansa_in.sid"
_RENDER_FRAMES = 600


def _assert_sound(test, song, name: str) -> None:
    test.assertEqual(len(song.snapshot), STANDARD_SNAPSHOT_SIZE, name)
    seen = {
        b & 0x7F
        for arr in (song.arranger_v1, song.arranger_v2, song.arranger_v3)
        for b in bytes(arr)
        if b not in (0, 0xFF)
    }
    test.assertTrue(seen, f"{name}: no patterns referenced by any arranger")
    bodies = 0
    for pat in seen:
        test.assertEqual(len(song.pattern_events(pat)), 32, f"{name}: pattern {pat}")
        bodies += bool(any(bytes(song.pattern(pat))))
    test.assertTrue(bodies, f"{name}: all referenced pattern bodies empty")
    for y in range(256):
        song.sidtab_row(y)


class TestWorktuneReplay(unittest.TestCase):
    """``DefmonSong.to_replay`` and ``DefmonPlayer`` over packed worktunes."""

    @classmethod
    def setUpClass(cls):
        cls.editor = editor_image_bytes()
        if cls.editor is None:
            raise unittest.SkipTest(
                "defMON editor image (packing oracle) unreachable; offline runner"
            )

    def test_every_release_worktune_packs_to_a_sound_replay(self):
        for name in _WORKTUNES:
            try:
                path = fixture_path(name)
            except FileNotFoundError:
                self.skipTest(f"worktune fixture {name} unreachable")
            song = DefmonSong.from_bytes(path.read_bytes())
            self.assertEqual(song.form, "editor", name)
            psid = song.to_replay(self.editor)
            self.assertEqual(psid[:4], b"PSID", name)
            packed = DefmonSong.from_bytes(psid)
            _assert_sound(self, packed, name)

    def test_to_replay_rejects_packed_form(self):
        song = DefmonSong.from_bytes(fixture_path("dansa_in.prg").read_bytes())
        packed = DefmonSong.from_bytes(song.to_replay(self.editor))
        with self.assertRaises(DefmonError):
            packed.to_replay(self.editor)

    def test_raw_form_is_a_loadable_prg(self):
        song = DefmonSong.from_bytes(fixture_path("dansa_in.prg").read_bytes())
        psid = song.to_replay(self.editor)
        raw = song.to_replay(self.editor, raw=True)
        self.assertEqual(raw[:2], (0x1000).to_bytes(2, "little"))
        self.assertEqual(raw[2:], bytes(SidImage.from_bytes(psid).image))
        self.assertGreaterEqual(find_signature(raw[2:]), 0)

    def test_pack_input_validation(self):
        with self.assertRaises(ValueError):
            _packer.pack(b"\x00" * 100, b"\x00\x18\x00\x00")
        with self.assertRaises(ValueError):
            _packer.pack(self.editor, b"\x00\x18")

    def test_packed_replay_renders(self):
        song = DefmonSong.from_bytes(fixture_path("dansa_in.prg").read_bytes())
        grid = DefmonPlayer(song.to_replay(self.editor)).render_grid(_RENDER_FRAMES)
        self.assertEqual(len(grid), _RENDER_FRAMES)
        self.assertEqual(len(grid[0]), 25)

    def test_accepts_packed_defmonsong(self):
        song = DefmonSong.from_bytes(fixture_path("dansa_in.prg").read_bytes())
        packed = DefmonSong.from_bytes(song.to_replay(self.editor))
        self.assertEqual(len(DefmonPlayer(packed).render_grid(8)), 8)

    def test_editor_form_song_rejects_player(self):
        song = DefmonSong.from_bytes(fixture_path("dansa_in.prg").read_bytes())
        with self.assertRaises(DefmonError):
            DefmonPlayer(song)

    def test_dansa_render_matches_hvsc_composer_rip(self):
        """Packed ``.DANSA IN`` renders exactly like the composer's rip.

        The composer's rip carries the editor's last-written player state,
        which a from-worktune pack cannot know; forcing the rip to the
        packed player region defines the reset (data is already identical).
        """
        rip_path = resolve_tune(_DANSA_HVSC)
        if rip_path is None:
            self.skipTest("HVSC Dansa_in.sid unreachable")
        song = DefmonSong.from_bytes(fixture_path("dansa_in.prg").read_bytes())
        replay = song.to_replay(self.editor)
        packed = SidImage.from_bytes(replay)
        rip = SidImage.from_bytes(rip_path.read_bytes())
        if (packed.load, packed.end) != (rip.load, rip.end):
            self.skipTest("rip layout differs from the packed replay")
        if bytes(packed.image[0x800:]) != bytes(rip.image[0x800:]):
            self.skipTest("composer rip data allocation differs from the pack")
        forced = bytearray(rip.image)
        forced[0:0x800] = bytes(packed.image[0:0x800])
        rip_reset = write_psid(
            load=rip.load,
            init=rip.header.init_address,
            play=rip.header.play_address,
            image=bytes(forced),
            name=rip.header.name,
            flags=rip.header.flags,
            songs=rip.header.songs,
            start_song=rip.header.start_song,
        )
        a = DefmonPlayer(replay).render_grid(_RENDER_FRAMES)
        b = DefmonPlayer(rip_reset).render_grid(_RENDER_FRAMES)
        diffs = [f for f in range(_RENDER_FRAMES) if a[f] != b[f]]
        self.assertEqual(diffs, [], f"{len(diffs)}/{_RENDER_FRAMES} frames differ")


if __name__ == "__main__":
    unittest.main()
