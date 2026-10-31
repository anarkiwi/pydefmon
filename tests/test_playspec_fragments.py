"""defMON mechanism fragments on synthetic memory.

Expected bytes are derived by hand from the player's documented semantics
(``docs/format.md``).
"""

import pytest

from pysidtracker.playspec import compile_program, elaborate, parse
from pysidtracker.playspec.pyexec import controls

from pydefmon.spec import spec_text


class Run:
    """A fragment bound to ``binding``, stepping a 64 KiB memory one call at a time."""

    def __init__(self, name, binding, **regs):
        self.program = elaborate(parse(spec_text(name)), binding)
        self.frame = compile_program(self.program)
        self.state = controls(self.program)
        self.mem = bytearray(0x10000)
        self.regs = {k[4:]: 0 for k in self.program.inputs} | regs

    def poke(self, addr, *values):
        """Store ``values`` from ``addr``."""
        self.mem[addr : addr + len(values)] = bytes(values)
        return self

    def step(self, frames=1):
        """Run ``frames`` calls."""
        for _ in range(frames):
            self.frame(
                self.mem.__getitem__, self.mem.__setitem__, self.state, None, self.regs
            )
        return self

    def peek(self, addr, count=1):
        """``count`` bytes from ``addr`` (one int for a single byte)."""
        values = list(self.mem[addr : addr + count])
        return values[0] if count == 1 else values


def test_defmon_pw_sweep_clamps_and_flips():
    run = Run("defmon_pwsweep", {"depth": 0x1000, "pw_lo": 0x1010, "pw_hi": 0x1020})
    voice = [0x31 * v for v in range(3)]
    for v, depth, lo, hi in zip(
        voice, (0x84, 0x03, 0x90), (0xFE, 0x02, 0xF8), (0x0F, 0x00, 0x05)
    ):
        run.poke(0x1000 + v, depth).poke(0x1010 + v, lo).poke(0x1020 + v, hi)
    run.step()
    assert [run.peek(0x1000 + v) for v in voice] == [0x04, 0x83, 0x90]
    assert [run.peek(0x1010 + v) for v in voice] == [0xF8, 0x01, 0x08]
    assert run.peek(0x1020 + voice[2]) == 6
    run.step()
    assert run.peek(0x1010) == 0xF3 and run.peek(0x1010 + voice[1]) == 0x04


@pytest.mark.parametrize(
    "opcode, acc, step, expected",
    [
        (0x69, (0xF0, 0x00), (0x20, 0x00), 0x05),
        (0xE9, (0x10, 0x30), (0x05, 0x01), 0x30),
    ],
)
def test_defmon_acid_self_modified_adc_sbc(opcode, acc, step, expected):
    run = Run(
        "defmon_acid",
        {
            "acc_lo": 0x1000,
            "acc_hi": 0x1001,
            "step_lo": 0x1002,
            "step_hi": 0x1003,
            "opcode": 0x2000,
            "extra": 0x1004,
            "floor": 0x2011,
            "shift": 0x2012,
            "sid": 0xD400,
        },
    )
    run.poke(0x1000, *acc, *step).poke(0x2000, opcode).poke(0x2011, 0x05)
    assert run.step().peek(0xD416) == expected
