"""defMON mechanism fragments on synthetic memory.

Expected bytes are derived by hand from the player's documented semantics
(``docs/format.md``).
"""

from pathlib import Path

import pytest

from pysidtracker.playspec import compile_program, elaborate, parse
from pysidtracker.playspec.pyexec import controls

SPECS = Path(__file__).resolve().parent.parent / "specs"


class Run:
    """A fragment bound to ``binding``, stepping a 64 KiB memory one call at a time."""

    def __init__(self, name, binding, **regs):
        self.program = elaborate(parse((SPECS / f"{name}.play").read_text()), binding)
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


def test_defmon_counted_jump_plays_its_loop_count_plus_one():
    run = Run(
        "defmon_arranger",
        {
            "row": 0x1000,
            "loop": 0x1001,
            "v1": 0x1100,
            "v2": 0x1200,
            "v3": 0x1300,
            "pattern": 0x1010,
        },
    )
    run.poke(0x1000, 0xFF).poke(0x1100, 0x01, 0x81, 0x04).poke(0x1200, 0x02, 0x00, 0x05)
    run.poke(0x1300, 0x03, 0x02, 0x06)
    rows = [run.step().peek(0x1010) for _ in range(4)]
    assert rows == [1, 1, 1, 4] and run.peek(0x1001) == 0


def test_defmon_alt_ends_the_step_of_every_voice_next_tick():
    run = Run(
        "defmon_pattern",
        {
            "counter": 0x1000,
            "ptr_lo": 0x1010,
            "ptr_hi": 0x1020,
            "row_a": 0x1030,
            "row_b": 0x1040,
            "cnt_a": 0x1050,
            "cnt_b": 0x1060,
            "note": 0x1070,
            "base": 0x1080,
            "pending": 0x1090,
            "dur": 0x1091,
            "steps": 0x1092,
        },
    )
    run.poke(0x1000, 0, 5, 5).poke(0x1010, 0x00, 0x10, 0x20).poke(
        0x1020, 0x30, 0x30, 0x30
    )
    run.poke(0x3000, 0xD1, 0x07, 0x30).poke(0x1050, 9)
    run.step()
    assert run.peek(0x1000, 3) == [2, 4, 4]
    assert (run.peek(0x1030), run.peek(0x1050), run.peek(0x1070), run.peek(0x1080)) == (
        7,
        0,
        0x30,
        0x30,
    )
    assert (run.peek(0x1010), run.peek(0x1090), run.peek(0x1091)) == (3, 1, 1)
    run.step()
    assert (
        run.peek(0x1000, 3) == [1, 1, 1]
        and run.peek(0x1092) == 1
        and run.peek(0x1090) == 0
    )


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
            "sid": 0xD400,
        },
    )
    run.poke(0x1000, *acc, *step).poke(0x2000, opcode).poke(0x2011, 0x05)
    assert run.step().peek(0xD416) == expected


@pytest.mark.parametrize("stereo", [0, 1])
def test_defmon_main_tick_cadence_detune_and_stereo(stereo):
    run = Run(
        "defmon_nmi",
        {
            "nmis": 0x1000,
            "per_tick": 0x1001,
            "ticks": 0x1010,
            "note": 0x1020,
            "pitch_lo": 0x1100,
            "pitch_hi": 0x1200,
            "sid": 0xD400,
            "sid2": 0xD420,
            "stereo": stereo,
        },
    )
    run.poke(0x1001, 2).poke(0x1105, 0xFF).poke(0x1205, 0x12).poke(
        0x1020, 5, 5, 5, 5, 5, 5
    )
    run.step()
    assert run.peek(0x1010) == 0
    run.step()
    assert run.peek(0x1010, 6) == [1, 1, 1] + [stereo] * 3
    assert [run.peek(0xD400 + 7 * v, 2) for v in range(3)] == [
        [0xFF, 0x12],
        [0x00, 0x12],
        [0x01, 0x12],
    ]
    assert run.peek(0xD427, 2) == ([0x00, 0x12] if stereo else [0, 0])
