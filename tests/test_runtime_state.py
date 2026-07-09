"""Unit coverage for DefmonPlayer.import_runtime_state (live-RAM seeding).

``import_runtime_state`` bridges the player from a live C64 / VICE RAM read so
per-frame output can be compared against a running defMON instance (it is the
mechanism the Goto80 decoder's residual-operand seeding is a lightweight
static analogue of). The live-VICE integration test exercises it end to end but
is skipped without Docker; this pins its field mapping against a synthetic RAM
window so the byte layout it reads stays correct.
"""

import unittest

from pydefmon.defmon import DefmonSong, LOAD_ADDRESS, STANDARD_SNAPSHOT_END
from pydefmon.defmon_player import DefmonPlayer

_VOICE_X = (0x00, 0x31, 0x62)


class TestImportRuntimeState(unittest.TestCase):
    def _window(self):
        """A RAM window covering $1019..$7166 (below load addr through the
        snapshot region), pre-zeroed."""
        base = 0x1019
        ram = bytearray(STANDARD_SNAPSHOT_END - base)
        return base, ram

    def _put(self, ram, base, addr, val):
        ram[addr - base] = val & 0xFF

    def test_per_voice_and_global_fields(self):
        base, ram = self._window()
        # Distinct per-voice work-block values so the $31 stride is verified.
        for v_idx, vx in enumerate(_VOICE_X):
            tag = (v_idx + 1) * 0x10
            self._put(ram, base, 0x1019 + vx, tag + 1)  # slide_accum_lo
            self._put(ram, base, 0x101A + vx, tag + 2)  # slide_accum_hi
            self._put(ram, base, 0x101B + vx, tag + 3)  # slide_mode
            self._put(ram, base, 0x101E + vx, tag + 4)  # ps_depth
            self._put(ram, base, 0x1023 + vx, tag + 5)  # pulse_lo
            self._put(ram, base, 0x1025 + vx, tag + 6)  # pulse_hi
            self._put(ram, base, 0x102D + vx, tag + 7)  # freq_lo
            self._put(ram, base, 0x102F + vx, tag + 8)  # freq_hi
            self._put(ram, base, 0x1037 + vx, tag + 9)  # sr
            self._put(ram, base, 0x1039 + vx, tag + 10)  # ad
            self._put(ram, base, 0x103B + vx, tag + 11)  # ctrl_main
            self._put(ram, base, 0x103D + vx, tag + 12)  # ctrl_eor
            self._put(ram, base, 0x137F + vx, tag + 13)  # current_note
            self._put(ram, base, 0x12ED + vx, tag + 14)  # transpose buffer
        # Global filter / cutoff / song-position state.
        self._put(ram, base, 0x10AA, 0x55)  # filter_res_routing
        self._put(ram, base, 0x10AF, 0xA0)  # volume_mode
        self._put(ram, base, 0x10CE, 0x02)  # cutoff_floor
        self._put(ram, base, 0x10B8, 0x69)  # ADC opcode -> op_is_adc True
        self._put(ram, base, 0x10D4, 0x0A)  # ASL output opcode
        self._put(ram, base, 0x10D9, 0x7F)  # silence_flag
        self._put(ram, base, 0x10EB, 0x03)  # arranger_idx
        self._put(ram, base, 0x14EC, 0x09)  # jp_armed_row
        self._put(ram, base, 0x14ED, 0x02)  # jp_count

        player = DefmonPlayer(DefmonSong())
        player.import_runtime_state(bytes(ram), base)

        for v_idx in range(3):
            tag = (v_idx + 1) * 0x10
            v = player.voices[v_idx]
            self.assertEqual(v.slide_accum_lo, tag + 1)
            self.assertEqual(v.pulse_lo, tag + 5)
            self.assertEqual(v.pulse_hi, tag + 6)
            self.assertEqual(v.freq_lo, tag + 7)
            self.assertEqual(v.freq_hi, tag + 8)
            self.assertEqual(v.ctrl_main, tag + 11)
            self.assertEqual(v.ctrl_eor, tag + 12)
            self.assertEqual(v.current_note, tag + 13)
            self.assertEqual(player.transpose_buffers[v_idx], tag + 14)

        self.assertEqual(player.filter_res_routing, 0x55)
        self.assertEqual(player.volume_mode, 0xA0)
        self.assertTrue(player.cutoff_op_is_adc)
        self.assertTrue(player.cutoff_output_asl)
        self.assertEqual(player.silence_flag, 0x7F)
        self.assertEqual(player.arranger_idx, 0x03)
        self.assertEqual(player.jp_armed_row, 0x09)
        self.assertEqual(player.jp_count, 0x02)

    def test_snapshot_overlap_copied(self):
        # Window must start at/below $1019 (the per-voice reads) yet extend
        # through the snapshot region to exercise the overlap copy.
        base = 0x1019
        ram = bytearray(STANDARD_SNAPSHOT_END - base)
        ram[0x1F00 - base] = 0x91  # a pattern-body byte
        ram[0x5F00 - base] = 0x42  # a sidTAB-row byte
        player = DefmonPlayer(DefmonSong())
        player.import_runtime_state(bytes(ram), base)
        self.assertEqual(player.snapshot[0x1F00 - LOAD_ADDRESS], 0x91)
        self.assertEqual(player.snapshot[0x5F00 - LOAD_ADDRESS], 0x42)

    def test_address_outside_window_raises(self):
        base = 0x2000  # too high: $1019 fields fall outside the window
        ram = bytearray(0x100)
        player = DefmonPlayer(DefmonSong())
        with self.assertRaises(ValueError):
            player.import_runtime_state(bytes(ram), base)


if __name__ == "__main__":
    unittest.main()
