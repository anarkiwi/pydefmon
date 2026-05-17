"""Unit tests for the $1469 portamento path in
``pydefmon_player._pitch_slide_voice``.

Expected freq_lo / freq_hi values are hand-computed against the LUTs
embedded in ``defmon_player`` (NOTE_PITCH_LO/HI + the 576-byte
``_LUT_BLOCK`` that aliases the $14F8..$1737 backing region in the
static binary). The 6502 reference is ``_1469`` in
``defmon-src/tools/re/defmon.s``; this test verifies the Python
translation byte-for-byte against the disassembly arithmetic.
"""

from __future__ import annotations

import os
import unittest

from pydefmon.defmon import DefmonSong
from pydefmon.defmon_player import (
    DefmonPlayer,
    NOTE_PITCH_HI,
    NOTE_PITCH_LO,
    _lut,
)

GLOW_WORM = os.path.join(
    os.path.dirname(__file__),
    "..",
    "fixtures",
    "glow_worm.prg",
)


def _expected_portamento(note: int, slide_mode: int) -> tuple[int, int]:
    """Hand-computed mirror of the $1469 body (used to cross-check the
    player against an independent restatement of the algorithm)."""
    target = (slide_mode + note) & 0xFF

    a_lo = _lut(0x1584, target)
    m_lo = _lut(0x1583, target)
    sub_lo = a_lo - m_lo
    step_lo = sub_lo & 0xFF
    c_out_lo = 1 if sub_lo >= 0 else 0

    a_hi = _lut(0x1620, target)
    m_hi = _lut(0x161F, target)
    sub_hi = a_hi - m_hi - (1 - c_out_lo)
    step_hi = sub_hi & 0xFF

    pitch_lo = NOTE_PITCH_LO[note]
    pitch_hi = NOTE_PITCH_HI[note]
    total_lo = pitch_lo + step_lo
    carry = total_lo >> 8
    freq_lo = total_lo & 0xFF
    freq_hi = (pitch_hi + step_hi + carry) & 0xFF
    return freq_lo, freq_hi


class PortamentoPathTests(unittest.TestCase):
    """Drives ``_pitch_slide_voice`` directly with crafted voice
    state -- no song load needed, so we can pin individual code paths."""

    def setUp(self):
        if not os.path.exists(GLOW_WORM):
            self.skipTest(f"missing fixture: {GLOW_WORM}")
        # Minimal player init -- needs a real song so __init__ can
        # build per-voice state, but we ignore the song's contents
        # and poke voice fields directly.
        self.player = DefmonPlayer(DefmonSong.from_file(GLOW_WORM))

    def _run(self, v_idx: int, *, note: int, slide_mode: int) -> tuple[int, int]:
        v = self.player.voices[v_idx]
        v.current_note = note
        v.slide_mode = slide_mode
        v.pitch_base = (
            0  # portamento path ignores pitch_base, but keep it deterministic.
        )
        v.slide_accum_lo = 0
        v.slide_accum_hi = 0
        self.player._pitch_slide_voice(v_idx)
        return v.freq_lo, v.freq_hi

    def test_portamento_mid_range_3_semitone(self):
        # current_note=$24, slide_mode=$03. Target=$27=39.
        # By the embedded LUT: $1584,39=$a6 / $1583,39=$9c -> step_lo=$0a.
        # $1620,39=$01 / $161F,39=$01 -> step_hi=$00.
        # NOTE_PITCH_LO[$24]=$2d, NOTE_PITCH_HI[$24]=$02.
        # freq_lo=$37, freq_hi=$02.
        freq_lo, freq_hi = self._run(0, note=0x24, slide_mode=0x03)
        self.assertEqual(freq_lo, 0x37)
        self.assertEqual(freq_hi, 0x02)

    def test_portamento_matches_independent_restatement_grid(self):
        # Sweep a representative grid of (note, slide_mode) pairs that
        # span the usable musical range. Confirms _pitch_slide_voice
        # matches the hand-restated arithmetic byte-for-byte.
        for note in (0x18, 0x24, 0x30, 0x40, 0x50, 0x60, 0x70):
            for slide_mode in (0x01, 0x03, 0x05, 0x08, 0x0C, 0x10, 0x20, 0x40, 0x7F):
                with self.subTest(note=note, slide_mode=slide_mode):
                    got = self._run(0, note=note, slide_mode=slide_mode)
                    want = _expected_portamento(note, slide_mode)
                    self.assertEqual(got, want)

    def test_portamento_does_not_touch_slide_accumulator(self):
        # The $1469 body reads neither $1019,x nor $101A,x and does
        # not write them either. Confirm we don't accidentally clobber
        # the accumulator on the portamento path.
        v = self.player.voices[1]
        v.current_note = 0x30
        v.slide_mode = 0x05
        v.pitch_base = 0
        v.slide_accum_lo = 0xAB
        v.slide_accum_hi = 0xCD
        self.player._pitch_slide_voice(1)
        self.assertEqual(v.slide_accum_lo, 0xAB)
        self.assertEqual(v.slide_accum_hi, 0xCD)

    def test_portamento_pitch_independent_of_pitch_base(self):
        # No-slide path adds $101F,x (pitch_base) to the freq; the
        # portamento path does NOT. Confirm changing pitch_base does
        # not move the portamento freq.
        a = self._run(2, note=0x30, slide_mode=0x05)
        v = self.player.voices[2]
        v.pitch_base = 0x02  # V2's typical per-voice detune
        v.current_note = 0x30
        v.slide_mode = 0x05
        v.slide_accum_lo = 0
        v.slide_accum_hi = 0
        self.player._pitch_slide_voice(2)
        self.assertEqual((v.freq_lo, v.freq_hi), a)


class PortamentoIntegrationTests(unittest.TestCase):
    """End-to-end: run the player on the .GLOW WORM fixture for the
    frame range that previously diverged (1570+) and confirm we still
    emit per-frame writes -- i.e. the portamento path doesn't crash,
    wedge, or drift into silence."""

    def setUp(self):
        if not os.path.exists(GLOW_WORM):
            self.skipTest(f"missing fixture: {GLOW_WORM}")
        self.song = DefmonSong.from_file(GLOW_WORM)

    def test_advances_past_frame_1570_without_silence(self):
        player = DefmonPlayer(self.song)
        # Skip ahead to just past the divergence point.
        for _ in range(1600):
            player.play_frame()
        # At frame 1600 we should still be producing writes per frame.
        for _ in range(50):
            writes = player.play_frame()
            self.assertTrue(writes, "player produced no SID writes after frame 1600")


if __name__ == "__main__":
    unittest.main()
