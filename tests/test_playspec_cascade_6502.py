"""``specs/defmon_cascade.play`` against the replay's own sidTAB cascade.

The six cascade blocks run once on the CPU, with the row interpreter they call
replaced by an ``RTS``, and once as the spec on a copy of the same memory; the
whole 64 KiB, outside the stack page, must agree.
"""

import random

import pytest

from pysidtracker import SidImage
from pysidtracker.emu import run_to_rts_until
from pysidtracker.playspec import compile_program, elaborate, parse
from pysidtracker.playspec.pyexec import controls
from pysidtracker.testing import resolve_tune

from pydefmon import DefmonPlayer
from pydefmon.spec import (
    spec_text,
    CASCADE_BLOCKS,
    RTS,
    SIDROW_ENTRY,
    cascade_binding,
    player_base,
)

SPEC = parse(spec_text("defmon_cascade"))
# Replays assembled at $1000: elsewhere the jump lookup reads tables at the
# unrelocated address.
TUNES = (
    "MUSICIANS/D/Demsky_Martin/Acid_Bassline.sid",
    "MUSICIANS/D/Dex-D/Short_Dream.sid",
    "MUSICIANS/I/Ilkke/Klotokok.sid",
)
BLOCK = 0x31
BLOCKS = 6
STACK = range(0x100, 0x200)
CPU_SP = 0xF0


def _tune(relpath: str) -> bytes:
    path = resolve_tune(relpath)
    if path is None:
        pytest.skip(f"tune {relpath} unavailable (offline, not cached)")
    return path.read_bytes()


@pytest.mark.parametrize("relpath", TUNES)
def test_cascade_spec_equals_the_6502_blocks(relpath):
    data = _tune(relpath)
    image = SidImage.from_bytes(data)
    flat = bytearray(0x10000)
    flat[image.load : image.load + len(image.image)] = image.image
    base = player_base(flat, image.load, image.load + len(image.image))
    binding = cascade_binding(base)
    player = DefmonPlayer(data)
    # pylint: disable=protected-access
    mpu, bus, mem = player._mpu, player._obs, player._mem
    bus[base + SIDROW_ENTRY] = RTS
    first, after = (base + off for off in CASCADE_BLOCKS)
    program = elaborate(SPEC, binding)
    frame, rng = compile_program(program), random.Random(1)
    for _ in range(300):
        for k in range(BLOCKS):
            bus[binding["counter"] + k * BLOCK] = rng.choice(
                (0, 0, 1, 2, 0x7F, 0x80, 0xFF, rng.randrange(256))
            )
            bus[binding["rowidx"] + k * BLOCK] = rng.randrange(256)
        for name in ("rows_lo", "rows_hi", "dl"):
            for row in range(256):
                low_hi_zero = name == "rows_hi" and rng.random() < 0.2
                bus[binding[name] + row] = 0 if low_hi_zero else rng.randrange(1, 256)
        spec_mem = bytearray(mem)
        frame(
            spec_mem.__getitem__,
            spec_mem.__setitem__,
            controls(program),
            None,
            {name[4:]: 0 for name in program.inputs},
        )
        mpu._cache.clear()
        mpu.sp = CPU_SP
        assert run_to_rts_until(mpu, bus, first, 0, {after}, 100_000)
        differing = [
            hex(a) for a in range(0x10000) if a not in STACK and mem[a] != spec_mem[a]
        ]
        assert not differing, differing[:8]
