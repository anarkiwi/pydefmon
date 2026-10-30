"""``specs/defmon_pitch.play`` against the replay's own pitch computation.

One voice's pitch runs once on the CPU and once as the spec on a copy of the
same memory, with random slide mode, accumulator and note; the whole 64 KiB,
outside the stack page, must agree.
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
from pydefmon._spec_layout import PITCH_BLOCK, VOICE_STRIDE, pitch_binding, player_base

SPEC = parse(
    (Path(__file__).resolve().parent.parent / "specs" / "defmon_pitch.play").read_text()
)
# Replays assembled at $1000: the constant-detune path reads and writes absolute
# addresses that only match there.
TUNES = (
    "MUSICIANS/D/Demsky_Martin/Acid_Bassline.sid",
    "MUSICIANS/D/Dex-D/Short_Dream.sid",
    "MUSICIANS/I/Ilkke/Klotokok.sid",
)
STACK = range(0x100, 0x200)
CPU_SP = 0xF0
MODES = (0x00, 0x01, 0x18, 0x7F, 0x80, 0x81, 0xBF, 0xC0, 0xC1, 0xFF)


def _tune(relpath: str) -> bytes:
    path = resolve_tune(relpath)
    if path is None:
        pytest.skip(f"tune {relpath} unavailable (offline, not cached)")
    return path.read_bytes()


@pytest.mark.parametrize("relpath", TUNES)
def test_pitch_spec_equals_the_6502_block(relpath):
    data = _tune(relpath)
    image = SidImage.from_bytes(data)
    flat = bytearray(0x10000)
    flat[image.load : image.load + len(image.image)] = image.image
    base = player_base(flat, image.load, image.load + len(image.image))
    player = DefmonPlayer(data)
    # pylint: disable=protected-access
    mpu, bus, mem = player._mpu, player._obs, player._mem
    first, after = (base + off for off in PITCH_BLOCK)
    rng = random.Random(1)
    for _ in range(400):
        voice = rng.randrange(3)
        at = voice * VOICE_STRIDE
        binding = pitch_binding(base, voice)
        program = elaborate(SPEC, binding)
        bus[binding["mode"] + at] = rng.choice(MODES + (rng.randrange(256),))
        bus[binding["acc_lo"] + at] = rng.randrange(256)
        bus[binding["acc_hi"] + at] = rng.randrange(256)
        bus[binding["note"] + at] = rng.randrange(256)
        bus[binding["step_lo"]] = rng.randrange(256)
        bus[binding["step_hi"]] = rng.randrange(256)
        spec_mem = bytearray(mem)
        compile_program(program)(
            spec_mem.__getitem__,
            spec_mem.__setitem__,
            controls(program),
            None,
            {name[4:]: 0 for name in program.inputs},
        )
        mpu._cache.clear()
        mpu.sp, mpu.x = CPU_SP, at
        assert run_to_rts_until(mpu, bus, first, 0, {after}, 100_000)
        differing = [
            hex(a) for a in range(0x10000) if a not in STACK and mem[a] != spec_mem[a]
        ]
        assert not differing, differing[:8]
