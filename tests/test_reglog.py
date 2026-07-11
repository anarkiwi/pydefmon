"""Register-log surface: the shared ``py*`` convention, driven by DefmonPlayer.

pydefmon adds no bespoke register-log code -- it re-exports the shared
:mod:`pysidtracker.reglog` surface (``RegWrite`` / ``register_writes_from_player``
/ ``read_reglog`` / ``write_reglog``). These tests confirm a
:class:`~pydefmon.DefmonPlayer` frames into that surface with the expected
clocking. Driven by a runnable synthetic replay so the path runs offline.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest

from pysidtracker import (
    DEFAULT_WRITE_SPACING,
    SID_REG_COUNT,
    SidParseError,
)

from pydefmon import (
    RegWrite,
    read_reglog,
    register_writes_from_player,
    write_reglog,
)
from pydefmon.defmon_player import DefmonPlayer

from tests._support import synthetic_replay


class TestRegLog(unittest.TestCase):
    def setUp(self):
        self.replay = synthetic_replay()

    def _writes(self, max_frames, **kwargs):
        return list(
            register_writes_from_player(
                DefmonPlayer(self.replay), max_frames=max_frames, **kwargs
            )
        )

    def test_reg_offsets_in_range_and_baseline_at_clock_zero(self):
        """Every write rebases to a ``0..$18`` offset; the post-init register
        baseline is emitted at clock 0, spaced write_spacing apart."""
        writes = self._writes(3)
        self.assertTrue(all(0 <= w.reg <= 0x18 for w in writes))
        self.assertEqual(writes[0].clock, 0)
        self.assertEqual(writes[1].clock, DEFAULT_WRITE_SPACING)
        baseline = writes[:SID_REG_COUNT]
        self.assertEqual([w.reg for w in baseline], list(range(SID_REG_COUNT)))

    def test_frame_writes_follow_cadence(self):
        """Post-baseline frames start one play-routine cadence apart."""
        cpf = DefmonPlayer(self.replay).cycles_per_frame
        writes = self._writes(2, cycles_per_frame=cpf)
        after_baseline = [w for w in writes if w.clock >= cpf]
        self.assertTrue(after_baseline)
        self.assertEqual(min(w.clock for w in after_baseline), cpf)

    def test_clock_options_override_cadence(self):
        writes = self._writes(2, cycles_per_frame=1000, write_spacing=2)
        self.assertEqual(writes[1].clock, 2)
        self.assertEqual(min(w.clock for w in writes if w.clock >= 1000), 1000)

    def test_round_trip_text(self):
        writes = self._writes(10)
        buf = io.StringIO()
        write_reglog(writes, buf)
        buf.seek(0)
        self.assertEqual(read_reglog(buf), writes)

    def test_round_trip_path(self):
        writes = self._writes(5)
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


if __name__ == "__main__":
    unittest.main()
