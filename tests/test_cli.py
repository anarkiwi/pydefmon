"""CLI (``pydefmon`` = info / reglog / wav), driven by a synthetic replay.

The WAV render goes through the real shared engine
(:func:`pysidtracker.render_player_wav`, a core dependency), rendering a tiny
window so the test stays fast and hermetic.
"""

from __future__ import annotations

import unittest
import wave
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from pysidtracker import read_reglog

from pydefmon import cli

from tests._support import synthetic_replay


class TestCli(unittest.TestCase):
    def setUp(self):
        self.replay = synthetic_replay()

    def _sid(self, tmp):
        path = Path(tmp) / "synth.sid"
        path.write_bytes(self.replay)
        return path

    def test_info(self):
        with TemporaryDirectory() as tmp:
            sid = self._sid(tmp)
            with mock.patch("sys.stdout", new_callable=StringIO) as out:
                rc = cli.main(["info", str(sid)])
            self.assertEqual(rc, 0)
            self.assertIn("load:", out.getvalue())

    def test_reglog(self):
        with TemporaryDirectory() as tmp:
            sid = self._sid(tmp)
            out = Path(tmp) / "log.txt"
            rc = cli.main(["reglog", str(sid), str(out), "--seconds", "0.05"])
            self.assertEqual(rc, 0)
            writes = read_reglog(out)
            self.assertTrue(writes and all(0 <= w.reg <= 0x18 for w in writes))

    def test_wav(self):
        with TemporaryDirectory() as tmp:
            sid = self._sid(tmp)
            out = Path(tmp) / "out.wav"
            rc = cli.main(["wav", str(sid), str(out), "--seconds", "0.02"])
            self.assertEqual(rc, 0)
            with wave.open(str(out), "rb") as w:
                self.assertGreater(w.getnframes(), 0)
                self.assertEqual(w.getnchannels(), 1)

    def test_info_non_replay_errors(self):
        with TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.prg"
            bad.write_bytes(b"\x00\x18" + b"\x00" * 32)
            self.assertEqual(cli.main(["info", str(bad)]), 1)


if __name__ == "__main__":
    unittest.main()
