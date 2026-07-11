"""Register-log surface (``pydefmon.reglog``): the shared py* convention.

Mirrors ``pygoattracker`` / ``pymusicassembler`` register logs so the same
downstream tooling can consume a defMON tune's per-frame output. Driven by a
runnable synthetic replay so the log path is exercised offline.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest

from pysidtracker import SidParseError

from pydefmon import (
    RegWrite,
    iter_register_writes,
    read_reglog,
    write_reglog,
)
from pydefmon.defmon_player import DefmonPlayer
from pydefmon.reglog import SID_REGISTERS

from tests._support import synthetic_replay


class TestRegLog(unittest.TestCase):
    def setUp(self):
        self.replay = synthetic_replay()

    def test_reg_offsets_in_range_and_baseline_at_clock_zero(self):
        """Every write rebases to a ``0..$18`` offset; the post-init register
        baseline is emitted at clock 0."""
        writes = list(iter_register_writes(self.replay, max_frames=3))
        self.assertTrue(all(0 <= w.reg <= 0x18 for w in writes))
        # register_writes_from_player emits the 25-register baseline first,
        # spaced write_spacing apart starting at clock 0.
        self.assertEqual(writes[0].clock, 0)
        self.assertEqual(writes[1].clock, 16)
        baseline = writes[:SID_REGISTERS]
        self.assertEqual([w.reg for w in baseline], list(range(SID_REGISTERS)))

    def test_frame_writes_follow_cadence(self):
        """Post-baseline frames start one play-routine cadence apart."""
        cpf = DefmonPlayer(self.replay).cycles_per_frame
        writes = list(iter_register_writes(self.replay, max_frames=2))
        after_baseline = [w for w in writes if w.clock >= cpf]
        self.assertTrue(after_baseline)
        self.assertEqual(min(w.clock for w in after_baseline), cpf)

    def test_clock_options_override_cadence(self):
        writes = list(
            iter_register_writes(
                self.replay, max_frames=2, cycles_per_frame=1000, write_spacing=2
            )
        )
        self.assertEqual(writes[1].clock, 2)
        self.assertEqual(min(w.clock for w in writes if w.clock >= 1000), 1000)

    def test_round_trip_text(self):
        writes = list(iter_register_writes(self.replay, max_frames=10))
        buf = io.StringIO()
        write_reglog(writes, buf)
        buf.seek(0)
        self.assertEqual(read_reglog(buf), writes)

    def test_round_trip_path(self):
        writes = list(iter_register_writes(self.replay, max_frames=5))
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
