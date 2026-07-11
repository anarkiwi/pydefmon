"""The defMON :class:`~pysidtracker.SidFormat` plugs into the generic CLI.

pydefmon ships no CLI binary; it registers ``defmon_format`` on the
``pysidtracker.formats`` entry point so the shared ``pysidtracker`` tool drives
it. The format's pieces (metadata / describe / player) are unit-tested offline;
recognition + the full ``info`` / ``reglog`` / ``wav`` flow run through
``pysidtracker.maincli`` against a real HVSC replay when the corpus is reachable.
"""

from __future__ import annotations

import unittest
import wave
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from pysidtracker import formats as formats_mod
from pysidtracker import maincli

from pydefmon.defmon import DefmonSong
from pydefmon.format import _metadata, defmon_format

from tests._support import fixture_path, resolve_tune, synthetic_replay


def _real_defmon_sid_or_skip(test):
    try:
        return fixture_path("glow_worm.sid").read_bytes()
    except FileNotFoundError:
        pass
    path = resolve_tune("MUSICIANS/I/Ilkke/Glow_Worm.sid")
    if path is None:
        test.skipTest("no real defMON .sid reachable (offline)")
    return path.read_bytes()


class TestDefmonFormat(unittest.TestCase):
    def test_format_shape(self):
        fmt = defmon_format()
        self.assertEqual(fmt.name, "defmon")
        self.assertIsNotNone(fmt.parser)

    def test_metadata_from_replay_header(self):
        song = DefmonSong()
        song.image_bytes = synthetic_replay()  # a valid PSID header
        name, author, released, load, init, play = _metadata(song)
        self.assertEqual((load, init, play), (0x1000, 0x1000, 0x1020))

    def test_metadata_workfile_without_header(self):
        song = DefmonSong()  # no container bytes -> no header
        load = song.load_address
        self.assertEqual(_metadata(song), ("", "", "", load, load, load))

    def test_describe_reports_snapshot_size(self):
        (line,) = list(defmon_format().describe(DefmonSong()))
        self.assertEqual(line, "snapshot: 22887 bytes")

    def test_player_builds_from_replay_bytes(self):
        song = DefmonSong()
        song.image_bytes = synthetic_replay()
        player = defmon_format().player(song)
        self.assertEqual(len(player.render_grid(2)), 2)

    def test_entry_point_registered(self):
        names = [f.name for f in formats_mod.discover_formats()]
        if "defmon" not in names:
            self.skipTest("entry point not installed (run `pip install -e .`)")
        self.assertIn("defmon", names)


class TestDefmonFormatViaGenericCli(unittest.TestCase):
    """Recognition + info/reglog/wav through the generic pysidtracker CLI."""

    def setUp(self):
        self.raw = _real_defmon_sid_or_skip(self)
        self.fmt = defmon_format()
        self.parser = maincli.build_parser([self.fmt])

    def test_recognized_by_content(self):
        self.assertIs(formats_mod.recognize_format([self.fmt], self.raw), self.fmt)

    def _run(self, argv):
        args = self.parser.parse_args(argv)
        args.func(args)

    def test_info_reglog_wav(self):
        with TemporaryDirectory() as tmp:
            sid = Path(tmp) / "tune.sid"
            sid.write_bytes(self.raw)

            with mock.patch("sys.stdout", new_callable=StringIO) as out:
                self._run(["info", str(sid)])
            text = out.getvalue()
            self.assertIn("format:   defmon", text)
            self.assertIn("snapshot:", text)

            log = Path(tmp) / "out.reglog"
            self._run(["reglog", str(sid), str(log), "--seconds", "0.1"])
            self.assertTrue(log.read_text().strip())

            out_wav = Path(tmp) / "out.wav"
            self._run(["wav", str(sid), str(out_wav), "--seconds", "0.02"])
            with wave.open(str(out_wav), "rb") as w:
                self.assertGreater(w.getnframes(), 0)


if __name__ == "__main__":
    unittest.main()
