"""``specs/defmon_sidrow.play`` against the replay's own sidTAB row interpreter.

A random row is written into a real tune's memory and one voice's record is
randomised; the interpreter runs once on the CPU and once as the spec on a copy
of the same memory, and the whole 64 KiB, outside the stack page, must agree.
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
    ROW_POINTER,
    SIDROW_ENTRY,
    VOICE_STRIDE,
    player_base,
    sidrow_binding,
)

SPEC = parse(spec_text("defmon_sidrow"))
# Replays assembled at $1000: the interpreter's RE store jumps to an absolute
# address that only matches there.
TUNES = (
    "MUSICIANS/D/Demsky_Martin/Acid_Bassline.sid",
    "MUSICIANS/D/Dex-D/Short_Dream.sid",
    "MUSICIANS/I/Ilkke/Klotokok.sid",
)
ROW_AT = 0x3000
STACK = range(0x100, 0x200)
CPU_SP = 0xF0
PER_VOICE = (
    "wgh",
    "wgl",
    "ad",
    "sr",
    "note",
    "note_base",
    "af",
    "pw_lo",
    "pw_hi",
    "ps",
)
SHARED = ("res", "fv", "acc_lo", "acc_hi", "step_lo", "step_hi", "op_lo", "op_hi")


def _tune(relpath: str) -> bytes:
    path = resolve_tune(relpath)
    if path is None:
        pytest.skip(f"tune {relpath} unavailable (offline, not cached)")
    return path.read_bytes()


@pytest.mark.parametrize("relpath", TUNES)
def test_sidrow_spec_equals_the_6502_interpreter(relpath):
    data = _tune(relpath)
    image = SidImage.from_bytes(data)
    flat = bytearray(0x10000)
    flat[image.load : image.load + len(image.image)] = image.image
    base = player_base(flat, image.load, image.load + len(image.image))
    player = DefmonPlayer(data)
    # pylint: disable=protected-access
    mpu, bus, mem = player._mpu, player._obs, player._mem
    rng = random.Random(1)
    for _ in range(300):
        voice = rng.randrange(3)
        binding = sidrow_binding(mem, base)
        for name in PER_VOICE:
            bus[binding[name] + voice * VOICE_STRIDE] = rng.randrange(256)
        for name in SHARED:
            bus[binding[name]] = rng.randrange(256)
        for offset in range(16):
            bus[ROW_AT + offset] = rng.randrange(256)
        bus[ROW_POINTER[0]], bus[ROW_POINTER[1]] = ROW_AT & 0xFF, ROW_AT >> 8
        program = elaborate(SPEC, binding)
        spec_mem = bytearray(mem)
        compile_program(program)(
            spec_mem.__getitem__,
            spec_mem.__setitem__,
            controls(program),
            None,
            {"a": ROW_AT & 0xFF, "x": voice * VOICE_STRIDE},
        )
        mpu._cache.clear()
        mpu.sp, mpu.x = CPU_SP, voice * VOICE_STRIDE
        run_to_rts_until(mpu, bus, base + SIDROW_ENTRY, ROW_AT & 0xFF, set(), 100_000)
        differing = [
            hex(a) for a in range(0x10000) if a not in STACK and mem[a] != spec_mem[a]
        ]
        assert not differing, differing[:8]
