"""``specs/defmon_walker.play`` against the replay's own arranger walker.

Random arranger pages, pointer tables, cursor and loop state are written into a
real tune; the walker runs once on the CPU and once as the spec on a copy of the
same memory, and the whole 64 KiB, outside the stack page, must agree.
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
from pydefmon._spec_layout import spec_text, WALKER_BLOCK, player_base, walker_binding

SPEC = parse(spec_text("defmon_walker"))
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


def _arranger_page(rng):
    """256 arranger bytes, a quarter of them with bit 7 set (jump rows)."""
    return [
        rng.randrange(0x80, 0x100) if rng.random() < 0.25 else rng.randrange(0x80)
        for _ in range(256)
    ]


@pytest.mark.parametrize("relpath", TUNES)
def test_walker_spec_equals_the_6502_walker(relpath):
    data = _tune(relpath)
    image = SidImage.from_bytes(data)
    flat = bytearray(0x10000)
    flat[image.load : image.load + len(image.image)] = image.image
    base = player_base(flat, image.load, image.load + len(image.image))
    binding = walker_binding(base)
    player = DefmonPlayer(data)
    # pylint: disable=protected-access
    mpu, bus, mem = player._mpu, player._obs, player._mem
    first, after = (base + off for off in WALKER_BLOCK)
    program = elaborate(SPEC, binding)
    frame, rng = compile_program(program), random.Random(1)
    for _ in range(400):
        for name in ("v1", "v2", "v3"):
            for row, value in enumerate(_arranger_page(rng)):
                bus[binding[name] + row] = value
        for offset in range(256):
            bus[binding["ptr_lo"] + offset] = rng.randrange(256)
        bus[binding["trigger"]] = rng.choice((0x00, 0x7F, 0x80, 0x83, 0x8F, 0xFF))
        cursor = rng.randrange(256)
        bus[binding["cursor"]] = cursor
        bus[binding["owner"]] = rng.choice((cursor, cursor, 0, rng.randrange(256)))
        bus[binding["count"]] = rng.choice((0, 1, 2, rng.randrange(256)))
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
