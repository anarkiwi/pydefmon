"""Pack-save export PRGs (SHIFT+P ``raw``/``executable``), issue #34.

The ``$1000`` raw export is the packed replay; the ``$0801``
executable is that same replay behind a BASIC stub + host boot. Both
parse to the matching ``.sid``'s structural model and play byte-exactly like it.
"""

import unittest

from pysidtracker import SidImage, song_frames
from pysidtracker.header import write_psid
from pysidtracker.testing import resolve_tune

from pydefmon import _packer
from pydefmon._sid_format import find_signature, prg_replay
from pydefmon.defmon import STANDARD_SNAPSHOT_SIZE, DefmonParseError, DefmonSong
from pydefmon.defmon_player import DefmonPlayer
from pydefmon.format import _metadata

from tests._support import editor_image_bytes, fixture_path

_DANSA = "dansa_in.prg"
_SID = "MUSICIANS/G/Goto80/Dansa_in.sid"


def _pack_raw(editor: bytes, worktune: bytes) -> bytes:
    image, _end = _packer.pack(editor, worktune)
    return (0x1000).to_bytes(2, "little") + image


def _pack_exe(editor: bytes, worktune: bytes) -> bytes:
    """Build the ``$0801`` executable as defMON exports it (stub + boot + player)."""
    image, _end = _packer.pack(editor, worktune)
    stub = bytes([0x0B, 0x08, 0x01, 0x00, 0x9E, 0x32, 0x30, 0x36, 0x31])
    boot = editor[0xD874 : 0xD874 + 0xB6]
    body = bytearray(0x7FF + len(image))
    body[: len(stub)] = stub
    body[len(stub) : len(stub) + len(boot)] = boot
    # defMON bakes the editor tempo into the boot CIA latch immediates at export.
    body[0x68], body[0x6D], body[0x99] = editor[0x715A], editor[0x715B], editor[0x715C]
    body[0x7FF : 0x7FF + len(image)] = image
    return (0x0801).to_bytes(2, "little") + bytes(body)


class TestPackExport(unittest.TestCase):
    """from_bytes / DefmonPlayer over defMON pack-save export PRGs."""

    @classmethod
    def setUpClass(cls):
        editor = editor_image_bytes()
        if editor is None:
            raise unittest.SkipTest("defMON editor image (packing oracle) unreachable")
        try:
            worktune = fixture_path(_DANSA).read_bytes()
        except FileNotFoundError:
            raise unittest.SkipTest(f"worktune fixture {_DANSA} unreachable")
        cls.raw = _pack_raw(editor, worktune)
        cls.exe = _pack_exe(editor, worktune)

    def _sid(self) -> bytes:
        path = resolve_tune(_SID)
        if path is None:
            self.skipTest("HVSC Dansa_in.sid unreachable")
        return path.read_bytes()

    def test_raw_loads_and_carries_the_player(self):
        self.assertEqual(self.raw[:2], (0x1000).to_bytes(2, "little"))
        self.assertGreaterEqual(find_signature(self.raw[2:]), 0)
        self.assertEqual(prg_replay(self.raw), (0x1000, 0x1000))

    def test_exe_loads_and_carries_the_player(self):
        stub = bytes([0x0B, 0x08, 0x01, 0x00, 0x9E, 0x32, 0x30, 0x36, 0x31])
        self.assertEqual(self.exe[:2], (0x0801).to_bytes(2, "little"))
        self.assertEqual(self.exe[2:11], stub)
        self.assertEqual(prg_replay(self.exe), (0x0801, 0x1000))

    def test_raw_parses_to_packed_form(self):
        song = DefmonSong.from_bytes(self.raw)
        self.assertEqual(song.form, "packed")
        self.assertEqual(song.replay_layout, "editor")
        self.assertEqual(len(song.snapshot), STANDARD_SNAPSHOT_SIZE)
        self.assertEqual(song.image_bytes, self.raw)

    def test_raw_snapshot_matches_hvsc_sid(self):
        self.assertEqual(
            bytes(DefmonSong.from_bytes(self.raw).snapshot),
            bytes(DefmonSong.from_bytes(self._sid()).snapshot),
        )

    def test_exe_snapshot_matches_hvsc_sid(self):
        self.assertEqual(
            bytes(DefmonSong.from_bytes(self.exe).snapshot),
            bytes(DefmonSong.from_bytes(self._sid()).snapshot),
        )

    def test_raw_player_renders(self):
        frames = song_frames(self.raw, None)
        grid = DefmonPlayer(self.raw).render_grid(frames)
        self.assertEqual(len(grid), frames)
        self.assertEqual(len(grid[0]), 25)

    def test_export_metadata_locates_the_player(self):
        raw = _metadata(DefmonSong.from_bytes(self.raw))
        exe = _metadata(DefmonSong.from_bytes(self.exe))
        self.assertEqual(raw, ("", "", "", 0x1000, 0x1000, 0x1003))
        self.assertEqual(exe, ("", "", "", 0x0801, 0x1000, 0x1003))

    def test_exe_player_renders_and_matches_raw(self):
        frames = song_frames(self.raw, None)
        self.assertEqual(
            DefmonPlayer(self.raw).render_grid(frames),
            DefmonPlayer(self.exe).render_grid(frames),
        )

    def test_sid_stripped_to_raw_prg_roundtrips(self):
        """A .sid with its header stripped, re-prefixed as a raw PRG, still parses."""
        data = self._sid()
        img = SidImage.from_bytes(data)
        prg = img.load.to_bytes(2, "little") + bytes(img.image)
        self.assertEqual(
            bytes(DefmonSong.from_bytes(prg).snapshot),
            bytes(DefmonSong.from_bytes(data).snapshot),
        )

    def test_non_defmon_prg_rejected(self):
        with self.assertRaises(DefmonParseError):
            DefmonSong.from_bytes((0x2000).to_bytes(2, "little") + b"\x00" * 32)

    def test_worktune_prg_still_editor_form(self):
        wt = fixture_path(_DANSA).read_bytes()
        self.assertEqual(DefmonSong.from_bytes(wt).form, "editor")
        with self.assertRaises(DefmonParseError):
            DefmonSong.from_bytes((0x2000).to_bytes(2, "little") + wt[2:])


class TestPackExportPlayer(unittest.TestCase):
    """PSID-wrapped and bare-PRG playback of the same replay agree."""

    @classmethod
    def setUpClass(cls):
        editor = editor_image_bytes()
        if editor is None:
            raise unittest.SkipTest("defMON editor image (packing oracle) unreachable")
        try:
            worktune = fixture_path(_DANSA).read_bytes()
        except FileNotFoundError:
            raise unittest.SkipTest(f"worktune fixture {_DANSA} unreachable")
        image, _end = _packer.pack(editor, worktune)
        cls.psid = write_psid(
            load=0x1000, init=0x1000, play=0x1003, image=image, flags=0x20
        )

    def test_psid_and_raw_prg_grids_match(self):
        prg = (0x1000).to_bytes(2, "little") + bytes(
            SidImage.from_bytes(self.psid).image
        )
        frames = song_frames(self.psid, None)
        self.assertEqual(
            DefmonPlayer(self.psid).render_grid(frames),
            DefmonPlayer(prg).render_grid(frames),
        )


if __name__ == "__main__":
    unittest.main()
