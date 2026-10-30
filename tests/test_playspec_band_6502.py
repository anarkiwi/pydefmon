"""``specs/defmon_band.play`` against the replay's own SID write band.

Random voice records and routing and filter-mode operands are written into a real
tune; the band runs once on the CPU and once as the spec on a copy of the same
memory, and the whole 64 KiB, outside the stack page, must agree.
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
    BAND_BLOCK,
    BAND_OFFSETS,
    VOICE_STRIDE,
    band_binding,
    player_base,
)

SPEC = parse(spec_text("defmon_band"))
TUNES = (
    "MUSICIANS/I/Ilkke/Glow_Worm.sid",
    "MUSICIANS/D/Demsky_Martin/Acid_Bassline.sid",
    "MUSICIANS/D/Dex-D/Short_Dream.sid",
    "MUSICIANS/I/Ilkke/Klotokok.sid",
)
STACK = range(0x100, 0x200)
CPU_SP = 0xF0


def _tune(relpath: str) -> bytes:
    path = resolve_tune(relpath)
    if path is None:
        pytest.skip(f"tune {relpath} unavailable (offline, not cached)")
    return path.read_bytes()


@pytest.mark.parametrize("relpath", TUNES)
def test_band_spec_equals_the_6502_band(relpath):
    data = _tune(relpath)
    image = SidImage.from_bytes(data)
    flat = bytearray(0x10000)
    flat[image.load : image.load + len(image.image)] = image.image
    base = player_base(flat, image.load, image.load + len(image.image))
    binding = band_binding(base)
    player = DefmonPlayer(data)
    # pylint: disable=protected-access
    mpu, bus, mem = player._mpu, player._obs, player._mem
    first, after = (base + off for off in BAND_BLOCK)
    program = elaborate(SPEC, binding)
    frame, rng = compile_program(program), random.Random(1)
    for _ in range(300):
        for name in BAND_OFFSETS:
            for voice in range(3):
                bus[binding[name] + voice * VOICE_STRIDE] = rng.randrange(256)
        for register in range(0x19):
            bus[binding["sid"] + register] = rng.randrange(256)
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
