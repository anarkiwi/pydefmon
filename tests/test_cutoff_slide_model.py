"""Unit tests for the $10B5-$10D7 filter cutoff slide model in
``DefmonPlayer._cutoff_slide_step`` — verified directly against the
defmon.s assembly (github.com/anarkiwi/undefmon), independent of
cascade dispatch and sidTAB row apply.

The assembly under test, with addresses called out from defmon.s:

    $10B5  LDA #acc_lo       ; A := acc_lo operand
    $10B7  CLC               ; C := 0
    $10B8  ADC/SBC #step_lo  ; SMC: $69=ADC, $E9=SBC
    $10BA  STA acc_lo        ; new acc_lo
    $10BD  LDA #acc_hi
    $10BF  ADC/SBC #step_hi  ; SMC: matches the lo opcode
    $10C1  BPL skip          ; positive => keep A; else reload floor
    $10C3  LDA floor         ; reload
    $10C6  STA acc_hi        ; new acc_hi (= A from BPL path or floor)
    $10C9  ADC #cutoff_extra ; A += extra + high_carry
    $10CB  BMI reload2
    $10CD  CMP #$02          ; threshold_reload literal
    $10CF  BCS output        ; A >= $02 => output A
    $10D1  LDA floor         ; A := floor
    $10D4  ASL A / NOP       ; SMC: $0A=ASL, $EA=NOP
    $10D5  STA $D416

Carry semantics: CLC at $10B7 forces ADC carry-in to 0; SBC with
carry-in 0 subtracts an extra 1 (= ``A - operand - 1``). LDA / STA
don't touch carry, so the lo->hi and hi->saturation carry chain is
preserved.
"""

from __future__ import annotations

import unittest

from pydefmon.defmon import DefmonSong
from pydefmon.defmon_player import DefmonPlayer


def _bare_player() -> DefmonPlayer:
    """Empty-song player with neutral defaults for direct cutoff
    state poking. The song fixture is irrelevant — we exercise only
    ``_cutoff_slide_step``."""
    return DefmonPlayer(DefmonSong())


class TestCutoffSlideAdc(unittest.TestCase):
    """ADC mode ($10B8/$10BF opcodes = $69): cutoff accumulator
    integrates +step_hi per frame; output = acc_hi + cutoff_extra."""

    def test_adc_no_extra_zero_step(self) -> None:
        p = _bare_player()
        p.cutoff_acc_lo = 0x00
        p.cutoff_acc_hi = 0x10
        p.cutoff_step_lo = 0x00
        p.cutoff_step_hi = 0x00
        p.cutoff_extra = 0x00
        p.cutoff_floor = 0x02
        p.cutoff_op_is_adc = True
        p.cutoff_output_asl = False
        # No change per frame. Output = acc_hi.
        for _ in range(5):
            self.assertEqual(p._cutoff_slide_step(), 0x10)

    def test_adc_step_hi_2_monotonic(self) -> None:
        p = _bare_player()
        p.cutoff_acc_lo = 0x00
        p.cutoff_acc_hi = 0x10
        p.cutoff_step_lo = 0x00
        p.cutoff_step_hi = 0x02
        p.cutoff_extra = 0x00
        p.cutoff_floor = 0x02
        p.cutoff_op_is_adc = True
        p.cutoff_output_asl = False
        # Per-frame: acc_hi += 2, output = acc_hi (extra=0).
        observed = [p._cutoff_slide_step() for _ in range(5)]
        self.assertEqual(observed, [0x12, 0x14, 0x16, 0x18, 0x1A])

    def test_adc_with_cutoff_extra(self) -> None:
        p = _bare_player()
        p.cutoff_acc_lo = 0x00
        p.cutoff_acc_hi = 0x10
        p.cutoff_step_lo = 0x00
        p.cutoff_step_hi = 0x02
        p.cutoff_extra = 0x04
        p.cutoff_floor = 0x02
        p.cutoff_op_is_adc = True
        p.cutoff_output_asl = False
        # Per-frame: acc_hi += 2, output = acc_hi + 4.
        observed = [p._cutoff_slide_step() for _ in range(4)]
        self.assertEqual(observed, [0x16, 0x18, 0x1A, 0x1C])

    def test_adc_lo_carry_propagates_to_hi(self) -> None:
        p = _bare_player()
        p.cutoff_acc_lo = 0xFE
        p.cutoff_acc_hi = 0x10
        p.cutoff_step_lo = 0x04  # 0xFE + 4 = 0x102 -> carry out
        p.cutoff_step_hi = 0x00
        p.cutoff_extra = 0x00
        p.cutoff_floor = 0x02
        p.cutoff_op_is_adc = True
        p.cutoff_output_asl = False
        # Frame 1: lo = 0x02, carry=1; hi += 1 -> 0x11. Output 0x11.
        self.assertEqual(p._cutoff_slide_step(), 0x11)
        # Frame 2: lo = 0x06, carry=0; hi unchanged. Output 0x11.
        self.assertEqual(p._cutoff_slide_step(), 0x11)

    def test_adc_negative_hi_reloads_floor(self) -> None:
        """When hi rolls into bit-7 territory (>= $80), the BPL at
        $10C1 fails and acc_hi is reloaded from floor ($02)."""
        p = _bare_player()
        p.cutoff_acc_lo = 0x00
        p.cutoff_acc_hi = 0x7E
        p.cutoff_step_lo = 0x00
        p.cutoff_step_hi = 0x02
        p.cutoff_extra = 0x00
        p.cutoff_floor = 0x02
        p.cutoff_op_is_adc = True
        p.cutoff_output_asl = False
        # Frame 1: hi = 0x7E + 2 = 0x80, bit 7 set -> reload to floor 0x02.
        # Output = floor + extra(0) = 0x02.
        self.assertEqual(p._cutoff_slide_step(), 0x02)
        # Frame 2: starts from acc_hi=0x02; hi = 0x02 + 2 = 0x04.
        self.assertEqual(p._cutoff_slide_step(), 0x04)


class TestCutoffSlideSbc(unittest.TestCase):
    """SBC mode ($10B8/$10BF opcodes = $E9): cutoff accumulator
    integrates -step_hi per frame (with an extra -1 from CLC carry-
    in semantics, per defmon.s $10B7 CLC)."""

    def test_sbc_step_hi_2_extra_minus_one(self) -> None:
        """CLC carry-in 0 + SBC = A - step - 1. So step_hi=2 actually
        decrements acc_hi by 3 per frame (2 + 1 from CLC)."""
        p = _bare_player()
        p.cutoff_acc_lo = 0x00
        p.cutoff_acc_hi = 0x20
        p.cutoff_step_lo = 0x00
        p.cutoff_step_hi = 0x02
        p.cutoff_extra = 0x00
        p.cutoff_floor = 0x02
        p.cutoff_op_is_adc = False
        p.cutoff_output_asl = False
        # Frame 1: lo = 0 - 0 - 1 = 0xFF, borrow set.
        # hi = 0x20 - 0x02 - 1 = 0x1D.
        # Output = hi + extra(0) + high_carry.
        # high_carry from SBC: 1 if (hi - step_hi - 1) >= 0 = 1 here.
        # So output = 0x1D + 0 + 1 = 0x1E.
        self.assertEqual(p._cutoff_slide_step(), 0x1E)


class TestCutoffSlideOutputAsl(unittest.TestCase):
    """SMC at $10D4 toggles the output between NOP (pass-through, max
    output $7F) and ASL (double, max output $FE)."""

    def test_asl_doubles_output(self) -> None:
        p = _bare_player()
        p.cutoff_acc_lo = 0x00
        p.cutoff_acc_hi = 0x10
        p.cutoff_step_lo = 0x00
        p.cutoff_step_hi = 0x00
        p.cutoff_extra = 0x00
        p.cutoff_floor = 0x02
        p.cutoff_op_is_adc = True
        p.cutoff_output_asl = True
        self.assertEqual(p._cutoff_slide_step(), 0x10 << 1)

    def test_nop_passes_through(self) -> None:
        p = _bare_player()
        p.cutoff_acc_lo = 0x00
        p.cutoff_acc_hi = 0x10
        p.cutoff_step_lo = 0x00
        p.cutoff_step_hi = 0x00
        p.cutoff_extra = 0x00
        p.cutoff_floor = 0x02
        p.cutoff_op_is_adc = True
        p.cutoff_output_asl = False
        self.assertEqual(p._cutoff_slide_step(), 0x10)


class TestCutoffSlideSaturation(unittest.TestCase):
    """Per $10CB BMI / $10CD CMP #$02 / $10CF BCS: output gets
    reloaded to floor when (acc_hi + extra + high_carry) has bit 7
    set OR is < $02."""

    def test_low_output_reloads_floor(self) -> None:
        p = _bare_player()
        # acc_hi = 0, extra = 0, step = 0 => A = 0 < $02 => reload floor.
        p.cutoff_acc_lo = 0x00
        p.cutoff_acc_hi = 0x00
        p.cutoff_step_lo = 0x00
        p.cutoff_step_hi = 0x00
        p.cutoff_extra = 0x00
        p.cutoff_floor = 0x05
        p.cutoff_op_is_adc = True
        p.cutoff_output_asl = False
        # Output saturates to floor.
        self.assertEqual(p._cutoff_slide_step(), 0x05)

    def test_negative_saturation_output_reloads_floor(self) -> None:
        p = _bare_player()
        # Force A bit 7 set in the saturation check. extra = $80 -> A wraps to high.
        p.cutoff_acc_lo = 0x00
        p.cutoff_acc_hi = 0x10
        p.cutoff_step_lo = 0x00
        p.cutoff_step_hi = 0x00
        p.cutoff_extra = 0x80
        p.cutoff_floor = 0x03
        p.cutoff_op_is_adc = True
        p.cutoff_output_asl = False
        # A = 0x10 + 0x80 = 0x90, bit 7 set -> reload floor.
        self.assertEqual(p._cutoff_slide_step(), 0x03)


if __name__ == "__main__":
    unittest.main()
