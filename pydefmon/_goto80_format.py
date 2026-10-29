"""defMON "Goto80" compact-runtime replay decoder (private).

A small family of early defMON ``.sid`` replays (the Goto80 / "Oxsid
Planetary" era builds, e.g. ``MUSICIANS/G/Goto80/{Evil_Wizard_2,
Rent-A-Cop, Rent-A-Cop_Reloaded, Rent-A-Cop_Reloaded_title}``) carry the
defMON replayer's SID-write signature but store their song data in an
OLDER, more compact runtime encoding than the fixed ``$1F00`` 4-byte
pattern rows the modern player (and :mod:`pydefmon._sid_format`) expects.

The data model is the *documented* defMON model (patterns = up to 32
steps of note + two sidCALL slots + a duration nibble; a per-voice
arranger with ``$FF`` jump rows; a sidTAB of variable-length bitmask
instrument programs with a DL/duration column) -- only the RUNTIME
*encoding* differs:

* **Patterns** are variable-length control-byte streams reached through
  a pointer pair (``patptr_lo[pat]`` / ``patptr_hi[pat]``). Each row is
  ``control`` byte + only the fields it changes. The control byte IS the
  editor flag byte: bit7 ``$80`` = end-of-pattern (ALT), bit6 ``$40`` =
  sidCALL1/GATE_A present, bit5 ``$20`` = sidCALL2/GATE_B present, bit4
  ``$10`` = note/GATE_N present, bits3-0 = row duration. Present fields
  follow in the order sidCALL1, sidCALL2, note. This mirrors defMON's
  ``$1147`` row-advance semantics exactly.
* **sidTAB rows** are the same variable-length bitmask instrument
  programs the modern player already interprets, reached through a
  ``sidtab_lo[row]`` / ``sidtab_hi[row]`` pointer pair with a DL byte at
  ``sidtab_dl[row]``. ``hi == 0`` marks a JP row whose ``lo`` is the
  target row index.
* **Arrangers** are three plain per-voice byte lists (``$FF`` jump row,
  ``$00`` silent) -- the editor layout verbatim.

:func:`decode` re-expands all of this into the editor-layout runtime RAM
image (``$1800..$7166``) that :class:`~pydefmon.defmon.DefmonSong` and
:class:`~pydefmon.defmon_player.DefmonPlayer` consume: pattern streams to
fixed ``$1F00`` 4-byte steps, sidTAB bodies relocated into the ``$5F00``
bank with rewritten ``$1800`` / ``$1900`` / ``$1E00`` tables, and the
arrangers copied to ``$1B00`` / ``$1C00`` / ``$1D00``.

The engine tables are located relocation-independently by matching the
build's own code idioms (the track-stepper's sidTAB pointer/DL loads and
the orderlist's pattern-pointer/arranger loads), so the exact relocation
address does not matter.
"""

from __future__ import annotations

from typing import Dict, Optional

from pysidtracker import ChainError, RoutineChain, SidImage, SitePattern

from pydefmon._layout import (
    LOAD_ADDRESS,
    SNAPSHOT_SIZE,
    relocate_sidtab_row,
    store_pattern,
)
from pydefmon._sid_format import (
    _SIG_SPEC as SIG_SPEC,
    _has_pattern_data,
    _referenced_patterns,
    find_signature,
)

# The player's own control flow, from the signature: the SID-write band of voice 1
# ends in ``JMP voice2``, voice 2's in ``JMP voice3`` (the two jumps hop over each
# voice's self-modified operand bytes). The data tables are read further on, so the
# table scan starts at ``voice3`` rather than anywhere in the image.
_VOICE2 = (
    "A2 ?? A9 ?? 8E 09 D4 8D 0A D4 A2 ?? A9 ?? 8E 07 D4 8D 08 D4 "
    "A2 ?? A0 ?? A9 ?? 49 ?? 8E 0D D4 8C 0C D4 8D 0B D4 4C {voice3:w}"
)
_CHAIN = RoutineChain(
    [
        ("entry", f"{SIG_SPEC} {{voice2:w}}"),
        ("voice2", _VOICE2),
    ]
)

# The table sites, one scan. The orderlist (per-voice pattern-pointer and arranger
# fetch), e.g. ``$88EC``: ``LDY / LDX arr_v0,Y / BPL / LDA arr_v1,Y / TAY / LDX
# arr_v0,Y`` then, per voice, ``LDA patptr_lo,X / STA op / LDA patptr_hi,X / STA op``
# with ``LDX arr_v1,Y`` / ``LDX arr_v2,Y`` ahead of voices 1 and 2. Some way on, the
# track-stepper idiom (per-voice sidTAB cascade fetch), e.g. ``$8ACF``: ``LDA
# sidtab_hi,Y / BNE / LDA sidtab_lo,Y / TAY / LDA ?,Y / STA $FC / LDA sidtab_dl,Y``.
_VOICE_PATPTR = "BD {patptr_lo:w} 8D ?? ?? BD {patptr_hi:w} 8D ?? ??"
_TABLES = SitePattern(
    "A0 ?? BE {arr_v0:w} 10 07 B9 ?? ?? A8 BE ?? ?? "
    f"{_VOICE_PATPTR} BE {{arr_v1:w}} {_VOICE_PATPTR} "
    f"BE {{arr_v2:w}} {_VOICE_PATPTR} "
    "??{0,1024} "
    "B9 {sidtab_hi:w} D0 07 B9 {sidtab_lo:w} A8 B9 ?? ?? 85 FC B9 {sidtab_dl:w}",
    first=True,
    span="image",
)


def find_tables(image: SidImage, signature: int) -> Optional[Dict[str, int]]:
    """Locate the Goto80 engine's data-table bases in ``image``, or ``None`` if the
    build's idioms are not at the places its player code leads to from the
    signature at ``signature``."""
    try:
        found = _CHAIN.discover(image, signature)
    except ChainError:
        return None
    hit = _TABLES.find_first(image, start=found["voice3"])
    if hit is None:
        return None
    return {
        name: hit.captures[name]
        for name in (
            "sidtab_lo",
            "sidtab_hi",
            "sidtab_dl",
            "patptr_lo",
            "patptr_hi",
            "arr_v0",
            "arr_v1",
            "arr_v2",
        )
    }


def _reconstruct(image: SidImage, tb: Dict[str, int]) -> bytearray:
    """Re-expand the Goto80 compact runtime data into the editor snapshot."""
    snap = bytearray(SNAPSHOT_SIZE)

    def w(addr: int, val: int) -> None:
        snap[addr - LOAD_ADDRESS] = val & 0xFF

    def rd(addr: int) -> int:
        return image.mem[addr & 0xFFFF]

    # Arrangers -> $1B00 / $1C00 / $1D00 (copied verbatim; the player walks
    # them only up to the $FF jump row, so trailing bytes are harmless).
    for base, dst in (
        (tb["arr_v0"], 0x1B00),
        (tb["arr_v1"], 0x1C00),
        (tb["arr_v2"], 0x1D00),
    ):
        for i in range(256):
            w(dst + i, rd(base + i))

    # Pattern streams -> fixed $1F00 4-byte editor steps. Only patterns
    # referenced by an arranger are materialised.
    for pat in _referenced_patterns(snap):
        addr = image.ptr(tb["patptr_lo"], tb["patptr_hi"], pat)
        if addr:
            store_pattern(snap, image, pat, addr)

    # sidTAB rows: relocate the variable-length bitmask body into the $5F00
    # bank and rewrite the runtime pointer pair + DL byte. hi == 0 => JP row
    # (lo is the target row index).
    for y in range(256):
        hi = rd(tb["sidtab_hi"] + y)
        lo = rd(tb["sidtab_lo"] + y)
        dl = rd(tb["sidtab_dl"] + y)
        if hi == 0:
            w(0x1900 + y, 0)
            w(0x1800 + y, lo)
            continue
        src = lo | (hi << 8)
        dst = relocate_sidtab_row(snap, image, y, src)
        w(0x1800 + y, dst & 0xFF)
        w(0x1900 + y, (dst >> 8) & 0xFF)
        w(0x1E00 + y, dl)

    return snap


def decode_replay(image: SidImage, signature: Optional[int] = None) -> Optional[bytes]:
    """Reconstruct the editor-layout snapshot from a Goto80 compact replay.

    Returns the 22887-byte ``$1800..$7166`` runtime RAM image, or ``None``
    if ``image`` is not a recognised Goto80-encoded defMON replay (no
    player signature, or the compact engine idioms are absent, or no
    arranger-referenced pattern produced a body). ``signature`` is the
    player-signature address when the caller already located it.
    """
    mem = image.mem
    if signature is None:
        signature = find_signature(mem)
    if signature < 0:
        return None
    tb = find_tables(image, signature)
    if tb is None:
        return None
    snap = _reconstruct(image, tb)
    if not _has_pattern_data(snap):
        return None
    return bytes(snap)
