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
  ``$1147`` row-advance semantics exactly (see
  :meth:`pydefmon.defmon_player.DefmonPlayer._pattern_advance`).
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

from typing import Dict, List, Optional

from pysidtracker import CodePattern, SidImage, find_code_all, find_code_first
from pysidtracker import registers as reg

from pydefmon._sid_format import (
    _has_pattern_data,
    _referenced_patterns,
    find_signature,
)

LOAD_ADDRESS = 0x1800
SNAPSHOT_END = 0x7167
SNAPSHOT_SIZE = SNAPSHOT_END - LOAD_ADDRESS  # 22887

_PATTERN_BANK = 0x1F00
_PATTERN_STRIDE = 0x80
_STEPS_PER_PATTERN = 32
_SIDTAB_BANK = 0x5F00
_SIDTAB_ROW_STRIDE = 15

_FLAG_ALT = 0x80
_FLAG_GATE_A = 0x40
_FLAG_GATE_B = 0x20
_FLAG_GATE_N = 0x10


# Track-stepper idiom (per-voice sidTAB cascade fetch), e.g. defMON-Goto80
# ``$8ACF``: LDA sidtab_hi,Y / BNE / LDA sidtab_lo,Y / TAY / LDA sidtab_hi,Y /
# STA $FC / LDA sidtab_dl,Y. The hi/lo/DL table bases are the abs operands.
_STEPPER = CodePattern(
    "B9 {sidtab_hi:w} D0 07 B9 {sidtab_lo:w} A8 B9 ?? ?? 85 FC B9 {sidtab_dl:w}"
)

# Orderlist idiom (per-voice pattern-pointer fetch), e.g. ``$88F8``:
# LDA patptr_lo,X / STA op / LDA patptr_hi,X. Appears once per voice with the
# same patptr_lo / patptr_hi bases.
_PATPTR = CodePattern("BD {patptr_lo:w} 8D ?? ?? BD {patptr_hi:w}")

# Per-voice arranger load with jump-skip, e.g. v0 ``$88EC``: LDX arr,Y / BPL.
_ARR_V0 = CodePattern("BE {arr_v0:w} 10 07")


def find_tables(mem: bytes) -> Optional[Dict[str, int]]:
    """Locate the Goto80 engine's data-table bases in ``mem`` (a 64 KiB
    image), or ``None`` if the build's idioms are not present."""
    st = find_code_first(mem, _STEPPER)
    if st is None:
        return None

    pp = find_code_first(mem, _PATPTR)
    if pp is None:
        return None
    patptr_lo = pp.captures["patptr_lo"]
    patptr_hi = pp.captures["patptr_hi"]

    # The three per-voice arranger LDX arr,Y loads. V0 is the one followed by
    # BPL+7 (jump-skip); V1/V2 are the two LDX arr,Y that directly precede a
    # patptr_lo load (BE aa aa BD patptr_lo).
    v0 = find_code_first(mem, _ARR_V0)
    if v0 is None:
        return None
    arr_v0 = v0.captures["arr_v0"]
    lo_b = patptr_lo & 0xFF
    lo_h = (patptr_lo >> 8) & 0xFF
    arr_after = f"BE {{arr:w}} BD {lo_b:02X} {lo_h:02X}"
    # The V0 jump-handler re-loads arr_v0 immediately before a patptr_lo load
    # too, so drop any arr_v0 hits; the first two distinct remaining bases are
    # V1 then V2 (their orderlist loads are contiguous BE arr,Y / BD patptr_lo).
    distinct: List[int] = []
    for match in find_code_all(mem, arr_after):
        base = match.captures["arr"]
        if base != arr_v0 and base not in distinct:
            distinct.append(base)
    if len(distinct) < 2:
        return None
    arr_v1, arr_v2 = distinct[0], distinct[1]

    return {
        "sidtab_lo": st.captures["sidtab_lo"],
        "sidtab_hi": st.captures["sidtab_hi"],
        "sidtab_dl": st.captures["sidtab_dl"],
        "patptr_lo": patptr_lo,
        "patptr_hi": patptr_hi,
        "arr_v0": arr_v0,
        "arr_v1": arr_v1,
        "arr_v2": arr_v2,
    }


def _reconstruct(mem: bytes, tb: Dict[str, int]) -> bytearray:
    """Re-expand the Goto80 compact runtime data into the editor snapshot."""
    snap = bytearray(SNAPSHOT_SIZE)

    def w(addr: int, val: int) -> None:
        snap[addr - LOAD_ADDRESS] = val & 0xFF

    def rd(addr: int) -> int:
        return mem[addr & 0xFFFF]

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
        addr = rd(tb["patptr_lo"] + pat) | (rd(tb["patptr_hi"] + pat) << 8)
        if addr == 0:
            continue
        p = addr
        for step in range(_STEPS_PER_PATTERN):
            control = rd(p)
            p = (p + 1) & 0xFFFF
            slot_a = slot_b = note = 0
            if control & _FLAG_GATE_A:
                slot_a = rd(p)
                p = (p + 1) & 0xFFFF
            if control & _FLAG_GATE_B:
                slot_b = rd(p)
                p = (p + 1) & 0xFFFF
            if control & _FLAG_GATE_N:
                note = rd(p)
                p = (p + 1) & 0xFFFF
            d = _PATTERN_BANK + pat * _PATTERN_STRIDE + step * 4 - LOAD_ADDRESS
            snap[d] = control
            snap[d + 1] = slot_a
            snap[d + 2] = slot_b
            snap[d + 3] = note
            if control & _FLAG_ALT:
                break

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
        dst = _SIDTAB_BANK + y * _SIDTAB_ROW_STRIDE
        for k in range(_SIDTAB_ROW_STRIDE):
            snap[dst + k - LOAD_ADDRESS] = rd(src + k)
        w(0x1800 + y, dst & 0xFF)
        w(0x1900 + y, (dst >> 8) & 0xFF)
        w(0x1E00 + y, dl)

    return snap


def decode_replay(image: SidImage) -> Optional[bytes]:
    """Reconstruct the editor-layout snapshot from a Goto80 compact replay.

    Returns the 22887-byte ``$1800..$7166`` runtime RAM image, or ``None``
    if ``image`` is not a recognised Goto80-encoded defMON replay (no
    player signature, or the compact engine idioms are absent, or no
    arranger-referenced pattern produced a body).
    """
    mem = image.mem
    if find_signature(mem) < 0:
        return None
    tb = find_tables(mem)
    if tb is None:
        return None
    snap = _reconstruct(mem, tb)
    if not _has_pattern_data(snap):
        return None
    return bytes(snap)


# Signature-relative work-block operand offsets (voice 0), from the SID-write
# band the signature anchors: PW lo/hi at sig+1/sig+3, FREQ lo/hi at
# sig+11/sig+13, and the 16-bit slide accumulator 10 bytes below PW lo. Voice
# v adds the $31 stride. defMON's init does NOT clear these, so a running tune
# inherits the loaded (author) values on frame 0.
_VOICE_STRIDE = 0x31


def seed_player_from_replay(player, raw: bytes) -> bool:
    """Seed a :class:`~pydefmon.defmon_player.DefmonPlayer`'s per-voice
    pulse-width, frequency and slide-accumulator operands from the residual
    work-block values a Goto80 replay loads (defMON's init leaves these
    non-zero). Returns ``True`` if the signature was found and seeding was
    applied. Needed for frame-0-onward parity because the player's output
    band emits the previous call's operands (output-then-compute)."""
    image = SidImage.from_bytes(raw)
    mem = image.mem
    sig = find_signature(mem)
    if sig < 0:
        return False
    for v_idx, voice in enumerate(player.voices):
        o = v_idx * _VOICE_STRIDE
        voice.pulse_lo = mem[(sig + 1 + o) & 0xFFFF]
        voice.pulse_hi = mem[(sig + 3 + o) & 0xFFFF]
        voice.freq_lo = mem[(sig + 11 + o) & 0xFFFF]
        voice.freq_hi = mem[(sig + 13 + o) & 0xFFFF]
        voice.slide_accum_lo = mem[(sig - 9 + o) & 0xFFFF]
        voice.slide_accum_hi = mem[(sig - 8 + o) & 0xFFFF]
    return True


# The SID register base is re-exported for callers that build absolute
# register addresses from :mod:`pysidtracker.registers` rather than literals.
SID_BASE = reg.SID_BASE
