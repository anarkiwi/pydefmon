"""``specs/defmon_nmi.play`` against the editor's own NMI handler.

The editor's NMI runs the replay once per interrupt: as the full tick (entry ``+3``)
when its sub-frame counter (``$7172``) is 0, else as the band-only call (entry
``+6``), and with stereo on (``$715D``) the second player copy (``$C800``) after it
the same way; then the counter counts to the ratio (``$715C``) and wraps. The
handler runs on the CPU with the four entries stubbed to ``RTS`` and the spec on a
copy of the same memory, from every counter, the ratios the editor offers and
random ones: the counter afterwards, and which entry ran, must agree.
"""

import random

import pytest

from pysidtracker.emu import wire_mpu
from pysidtracker.playspec import compile_program, elaborate, parse
from pysidtracker.playspec.pyexec import controls

from pydefmon.spec import spec_text

from tests._support import editor_image_bytes

COUNTER, RATIO, STEREO = 0x7172, 0x715C, 0x715D
NMI_HANDLER, RUNNING, RETURN_TO = 0x0AED, 0x0AF9, 0x0FF0
FULL, BAND_ONLY, SECOND_COPY = 0x1003, 0x1006, 0xB800
ENTRIES = (FULL, BAND_ONLY, FULL + SECOND_COPY, BAND_ONLY + SECOND_COPY)
TICK = 0x7000
RATIOS = (1, 2, 4, 8)
SPEC_TEXT = spec_text("defmon_nmi")


def _editor() -> bytes:
    image = editor_image_bytes()
    if image is None:
        pytest.skip("defMON editor image unavailable")
    return image


def _handler(image: bytes, ratio: int, counter: int, stereo: int):
    """The entries the editor's NMI handler ran and the counter it left."""
    mem = bytearray(image)
    mem[COUNTER], mem[RATIO], mem[STEREO] = counter, ratio, stereo
    mem[1], mem[RUNNING] = 0x35, 1
    for entry in ENTRIES:
        mem[entry] = 0x60
    mpu, bus = wire_mpu(mem, True, "6581")
    for byte in (RETURN_TO >> 8, RETURN_TO & 0xFF, 0x00):
        bus[0x100 + mpu.sp] = byte
        mpu.sp -= 1
    mpu.pc, entries = NMI_HANDLER, []
    while mpu.pc != RETURN_TO:
        if mpu.pc in ENTRIES:
            entries.append(mpu.pc)
        mpu.step()
    return entries, mem[COUNTER]


def _spec(text: str, ratio: int, counter: int):
    """The ``tick`` byte and counter the spec leaves."""
    program = elaborate(parse(text), {"counter": COUNTER, "ratio": RATIO, "tick": TICK})
    mem = bytearray(0x10000)
    mem[COUNTER], mem[RATIO] = counter, ratio
    compile_program(program)(
        mem.__getitem__,
        mem.__setitem__,
        controls(program),
        None,
        {name[4:]: 0 for name in program.inputs},
    )
    return mem[TICK], mem[COUNTER]


def _cases():
    rng = random.Random(1)
    for ratio in RATIOS:
        yield from ((ratio, counter) for counter in range(ratio))
    yield from ((rng.choice(RATIOS), rng.randrange(256)) for _ in range(24))
    yield from ((rng.randrange(1, 256), rng.randrange(256)) for _ in range(24))


@pytest.mark.parametrize("stereo", [0, 1])
def test_nmi_spec_equals_the_editors_nmi_handler(stereo):
    image = _editor()
    for ratio, counter in _cases():
        entries, after = _handler(image, ratio, counter, stereo)
        tick, spec_after = _spec(SPEC_TEXT, ratio, counter)
        assert entries[0] == (FULL if tick else BAND_ONLY), (ratio, counter)
        expected = [entries[0]] + [entries[0] + SECOND_COPY] * stereo
        assert entries == expected, (ratio, counter, stereo)
        assert spec_after == after, (ratio, counter)


@pytest.mark.parametrize(
    "old, new",
    [
        ("counter == 0", "counter == 1"),
        ("counter + 1", "counter + 2"),
        ("after == ratio", "after > ratio"),
        ("counter := 0;", "counter := 1;"),
    ],
)
def test_nmi_spec_mutations_are_caught(old, new):
    assert old in SPEC_TEXT
    image, mutated = _editor(), SPEC_TEXT.replace(old, new)
    assert any(
        _spec(mutated, ratio, counter)
        != (
            int(_handler(image, ratio, counter, 0)[0][0] == FULL),
            _handler(image, ratio, counter, 0)[1],
        )
        for ratio, counter in _cases()
    )
