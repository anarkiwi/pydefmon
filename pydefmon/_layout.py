"""Editor-layout constants and decode steps shared by the replay readers (private).

:mod:`pydefmon._sid_format` (editor-layout packed replays) and
:mod:`pydefmon._goto80_format` (the compact Goto80 runtime) both re-expand a
replay into the editor's ``$1800..$7166`` snapshot; the geometry of that
snapshot, the packed-pattern walk and the sidTAB row relocation live here.
"""

from __future__ import annotations

from typing import Callable, Iterator

LOAD_ADDRESS = 0x1800
SNAPSHOT_END = 0x7167
SNAPSHOT_SIZE = SNAPSHOT_END - LOAD_ADDRESS  # 22887

PATTERN_BANK = 0x1F00
PATTERN_STRIDE = 0x80
STEPS_PER_PATTERN = 32
SIDTAB_BANK = 0x5F00
SIDTAB_ROW_STRIDE = 15

FLAG_ALT = 0x80
FLAG_GATE_A = 0x40
FLAG_GATE_B = 0x20
FLAG_GATE_N = 0x10
GATE_BITS = (FLAG_GATE_A, FLAG_GATE_B, FLAG_GATE_N)  # payload order: a, b, note

Reader = Callable[[int], int]


def walk_pattern(rd: Reader, start: int) -> Iterator[bytes]:
    """Yield a packed pattern body's steps as editor-layout 4-byte events.

    Bodies are variable length: a flag byte then one payload byte per set gate
    bit (``$40`` slot_a, ``$20`` slot_b, ``$10`` note), which is exactly what
    the player's stream pointer advances by ($11BD: ``TYA / CLC / ADC $1186``).
    Stops after the ALT (``$80``) step, or at 32 steps. ``rd`` reads one byte at
    an absolute address.
    """
    pos = start
    for _ in range(STEPS_PER_PATTERN):
        flag = rd(pos)
        pos += 1
        event = bytearray(4)
        event[0] = flag
        for slot, gate in enumerate(GATE_BITS):
            if flag & gate:
                event[slot + 1] = rd(pos)
                pos += 1
        yield bytes(event)
        if flag & FLAG_ALT:
            return


def store_pattern(snap: bytearray, rd: Reader, pat: int, start: int) -> None:
    """Expand the packed body at ``start`` into pattern ``pat``'s fixed steps."""
    dst = PATTERN_BANK + pat * PATTERN_STRIDE - LOAD_ADDRESS
    for ev, event in enumerate(walk_pattern(rd, start)):
        d = dst + ev * 4
        if 0 <= d and d + 4 <= len(snap):
            snap[d : d + 4] = event


def relocate_sidtab_row(snap: bytearray, rd: Reader, y: int, src: int) -> int:
    """Copy sidTAB row ``y``'s body from ``src`` into its fixed ``$5F00`` slot.

    Returns the slot address (``$5F00 + y * 15``).
    """
    dst = SIDTAB_BANK + y * SIDTAB_ROW_STRIDE
    for k in range(SIDTAB_ROW_STRIDE):
        d = dst + k - LOAD_ADDRESS
        if 0 <= d < len(snap):
            snap[d] = rd(src + k)
    return dst
