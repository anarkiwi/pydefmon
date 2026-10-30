"""``specs/defmon_pwsweep.play`` against the replay's own pulse-width sweep.

Random depth and pulse width are written into a real tune's voice records; the
sweep of one voice runs once on the CPU and once as the spec on a copy of the
same memory, with the carry the entry leaves random, and depth and pulse width must agree.
"""

import random

import pytest

from pysidtracker import SidImage
from pysidtracker.emu import run_to_rts_until
from pysidtracker.playspec import compile_program, elaborate, parse
from pysidtracker.playspec.pyexec import controls
from pysidtracker.testing import resolve_tune

from pydefmon import DefmonPlayer
from pydefmon._spec_layout import spec_text, PWSWEEP_BLOCK, player_base, pwsweep_binding

SPEC = parse(spec_text("defmon_pwsweep"))
TUNES = (
    "MUSICIANS/I/Ilkke/Glow_Worm.sid",
    "MUSICIANS/D/Demsky_Martin/Acid_Bassline.sid",
    "MUSICIANS/D/Dex-D/Short_Dream.sid",
    "MUSICIANS/I/Ilkke/Klotokok.sid",
)
VOICE = 0x31
CARRY = 0x01
CPU_SP = 0xF0
EDGES = (0x00, 0x01, 0x7F, 0x80, 0x81, 0xFF, 0xF8, 0xF7, 0x0F)


def _tune(relpath: str) -> bytes:
    path = resolve_tune(relpath)
    if path is None:
        pytest.skip(f"tune {relpath} unavailable (offline, not cached)")
    return path.read_bytes()


@pytest.mark.parametrize("relpath", TUNES)
def test_pwsweep_spec_equals_the_6502_sweep(relpath):
    data = _tune(relpath)
    image = SidImage.from_bytes(data)
    flat = bytearray(0x10000)
    flat[image.load : image.load + len(image.image)] = image.image
    base = player_base(flat, image.load, image.load + len(image.image))
    binding = pwsweep_binding(base)
    player = DefmonPlayer(data)
    # pylint: disable=protected-access
    mpu, bus, mem = player._mpu, player._obs, player._mem
    first, after = (base + off for off in PWSWEEP_BLOCK)
    program = elaborate(SPEC, binding)
    frame, rng = compile_program(program), random.Random(1)
    for _ in range(300):
        voice = rng.randrange(3)
        at = {name: addr + voice * VOICE for name, addr in binding.items()}
        bus[at["depth"]] = rng.choice(EDGES + (rng.randrange(256),))
        bus[at["pw_lo"]] = rng.choice(EDGES + (rng.randrange(256),))
        bus[at["pw_hi"]] = rng.choice((0, 0x0F, rng.randrange(16)))
        carry = rng.random() < 0.5
        spec_mem = bytearray(mem)
        frame(
            spec_mem.__getitem__,
            spec_mem.__setitem__,
            controls(program),
            None,
            {"c": carry},
        )
        mpu._cache.clear()
        mpu.sp, mpu.x = CPU_SP, voice * VOICE
        mpu.p = mpu.p | CARRY if carry else mpu.p & ~CARRY
        assert run_to_rts_until(mpu, bus, first, 0, {after}, 100_000)
        cells = [at[name] for name in binding]
        assert [mem[c] for c in cells] == [spec_mem[c] for c in cells]
