"""``specs/defmon_reader.play`` against the replay's own pattern step reader.

One voice's reader runs once on the CPU and once as the spec on a copy of the
same memory, from random counter, pointer, gate and flag operands over a random
pattern stream; the whole 64 KiB, outside the stack page, must agree.
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
    READER_START,
    READER_STRIDE,
    player_base,
    reader_binding,
)

SPEC = parse(
    (
        Path(__file__).resolve().parent.parent / "specs" / "defmon_reader.play"
    ).read_text()
)
# Replays assembled at $1000: the reader advances its pointer's high byte
# through an absolute address that only matches there.
TUNES = (
    "MUSICIANS/D/Demsky_Martin/Acid_Bassline.sid",
    "MUSICIANS/D/Dex-D/Short_Dream.sid",
    "MUSICIANS/I/Ilkke/Klotokok.sid",
)
STREAM_AT = 0x3000
STACK = range(0x100, 0x200)
CPU_SP = 0xF0
# Random pointers stay in RAM: the spec is run on plain RAM, the CPU reads ROM above $A000.
COUNTERS = (0x00, 0x00, 0x01, 0x02, 0x7F, 0x80, 0xFF)


def _tune(relpath: str) -> bytes:
    path = resolve_tune(relpath)
    if path is None:
        pytest.skip(f"tune {relpath} unavailable (offline, not cached)")
    return path.read_bytes()


@pytest.mark.parametrize("relpath", TUNES)
def test_reader_spec_equals_the_6502_reader(relpath):
    data = _tune(relpath)
    image = SidImage.from_bytes(data)
    flat = bytearray(0x10000)
    flat[image.load : image.load + len(image.image)] = image.image
    base = player_base(flat, image.load, image.load + len(image.image))
    player = DefmonPlayer(data)
    # pylint: disable=protected-access
    mpu, bus, mem = player._mpu, player._obs, player._mem
    rng = random.Random(1)
    for _ in range(600):
        voice = rng.randrange(3)
        binding = reader_binding(base, voice)
        at = voice * READER_STRIDE
        bus[binding["counter"]] = rng.choice(COUNTERS + (rng.randrange(256),))
        for name in ("gate_a", "gate_b", "gate_n", "flag"):
            bus[binding[name]] = rng.randrange(256)
        bus[binding["trigger"]] = rng.randrange(256)
        for offset in range(8):
            bus[STREAM_AT + offset] = rng.randrange(256)
        pointer = STREAM_AT + rng.randrange(2)
        copies = rng.random() < 0.5
        for lo, hi, addr in (
            ("ptr_lo", "ptr_hi", pointer),
            (
                "copy1_lo",
                "copy1_hi",
                pointer if copies else rng.randrange(0x2000, 0xA000),
            ),
            (
                "copy2_lo",
                "copy2_hi",
                pointer if copies else rng.randrange(0x2000, 0xA000),
            ),
            (
                "copy3_lo",
                "copy3_hi",
                pointer if copies else rng.randrange(0x2000, 0xA000),
            ),
        ):
            bus[binding[lo]], bus[binding[hi]] = addr & 0xFF, addr >> 8
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
        start = base + READER_START + at
        assert run_to_rts_until(mpu, bus, start, 0, {start + READER_STRIDE}, 100_000)
        differing = [
            hex(a) for a in range(0x10000) if a not in STACK and mem[a] != spec_mem[a]
        ]
        assert not differing, (differing[:8], voice)
