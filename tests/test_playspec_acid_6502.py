"""``specs/defmon_acid.play`` against the replay's own 6502 cutoff-slide block.

Random operands are written into a real tune's code; the block runs once on the
CPU and once as the spec on a copy of the same memory, and the cutoff register
and the accumulator must agree.
"""

import random
from pathlib import Path

import pytest

from pysidtracker import SidImage
from pysidtracker.emu import run_to_rts_until
from pysidtracker.playspec import compile_program, elaborate, parse
from pysidtracker.playspec.pyexec import controls
from pysidtracker.testing import resolve_tune

from pydefmon import DefmonPlayer
from pydefmon._spec_layout import (
    spec_text,
    ACID_BLOCK,
    SID_BASE,
    acid_binding,
    player_base,
)

SPEC = parse(spec_text("defmon_acid"))
TUNES = (
    "MUSICIANS/I/Ilkke/Glow_Worm.sid",
    "MUSICIANS/D/Demsky_Martin/Acid_Bassline.sid",
    "MUSICIANS/D/Dex-D/Short_Dream.sid",
)
OPCODE_ADC, OPCODE_SBC, CLC = 0x69, 0xE9, 0x18
NOP, ASL_A = 0xEA, 0x0A
LO_CLC, LO_OP, HI_OP = 0xB7, 0xB8, 0xBF
CPU_SP = 0xF0


def _tune(relpath: str) -> bytes:
    path = resolve_tune(relpath)
    if path is None:
        pytest.skip(f"tune {relpath} unavailable (offline, not cached)")
    return path.read_bytes()


@pytest.mark.parametrize("relpath", TUNES)
def test_acid_spec_equals_the_6502_block(relpath):
    data = _tune(relpath)
    image = SidImage.from_bytes(data)
    flat = bytearray(0x10000)
    flat[image.load : image.load + len(image.image)] = image.image
    base = player_base(flat, image.load, image.load + len(image.image))
    binding = acid_binding(base)
    player = DefmonPlayer(data)
    # pylint: disable=protected-access
    mpu, bus, mem = player._mpu, player._obs, player._mem
    first, after = (base + off for off in ACID_BLOCK)
    rng = random.Random(1)
    for _ in range(200):
        for name in ("acc_lo", "step_lo", "acc_hi", "step_hi", "extra"):
            bus[binding[name]] = rng.randrange(256)
        bus[binding["floor"]] = rng.choice((0, 2, 5, 0x20))
        bus[binding["shift"]] = rng.choice((NOP, ASL_A))
        bus[base + LO_CLC] = CLC
        opcode = rng.choice((OPCODE_ADC, OPCODE_SBC))
        bus[base + LO_OP] = bus[base + HI_OP] = opcode
        program = elaborate(SPEC, binding)
        spec_mem = bytearray(mem)
        compile_program(program)(
            spec_mem.__getitem__,
            spec_mem.__setitem__,
            controls(program),
            None,
            {name[4:]: 0 for name in program.inputs},
        )
        mpu._cache.clear()
        mpu.sp = CPU_SP
        assert run_to_rts_until(mpu, bus, first, 0, {after}, 100_000)
        cells = (SID_BASE + 0x16, binding["acc_lo"], binding["acc_hi"])
        assert [mem[c] for c in cells] == [spec_mem[c] for c in cells]
