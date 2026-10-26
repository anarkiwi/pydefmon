"""defMON PSID/RSID packed-replay reader (private).

HVSC ships defMON tunes as PSID/RSID ``.sid`` *replay* files: a
relocatable player plus the tune's compacted data. This is a different
container from the ``.prg`` editor workfile that :mod:`pydefmon._load_format`
handles -- there is no ``$D6C9`` RLE stream here. The replay embeds the
player's runtime data tables directly (song-position pointers, sidTAB JP
markers, pattern-pointer cache, arrangers, DL bytes) but stores the
pattern bodies and sidTAB rows *compacted* (variable length, tightly
packed), addressed through the player's own pointer tables rather than at
the editor's fixed ``$1F00`` / ``$5F00`` strides.

:func:`depack_replay` recognises the replay by defMON's SID-write-band
signature, locates the data tables (the player is relocatable, so the base
is derived from the signature site), and re-expands the compacted pattern
bodies and sidTAB rows back into the editor-layout runtime RAM image
(``$1800..$7166``) that :class:`~pydefmon.defmon.DefmonSong` and
:class:`~pydefmon.defmon_player.DefmonPlayer` consume. The ``$D6C9`` codec
is not touched.

Layout facts (relative to the runtime data base ``DB`` == ``$1800`` for a
non-relocated replay):

* ``DB+$000``/``DB+$100`` -- per-sidTAB-row pointer lo/hi. When the hi byte
  is non-zero the pair is the *absolute* address of that row's compacted
  body; when hi is zero the row is a JP source and the lo byte is the JP
  target row index.
* ``DB+$200``/``DB+$280`` -- per-pattern pointer lo/hi (absolute address of
  the pattern's compacted body).
* ``DB+$300``/``DB+$400``/``DB+$500`` -- V1/V2/V3 arrangers.
* ``DB+$600`` -- per-sidTAB-row DL bytes.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from pysidtracker import CodePattern, SidImage, find_code_first, parse_prg
from pysidtracker import registers as reg

from pydefmon._layout import (
    LOAD_ADDRESS,
    PATTERN_BANK,
    PATTERN_STRIDE,
    SNAPSHOT_SIZE,
    relocate_sidtab_row,
    store_pattern,
)


def _sid_store(opcode: str, register: int) -> str:
    """Spec tokens for an absolute store to SID register ``$D400 + register``."""
    addr = reg.SID_BASE + register
    return f"{opcode} {addr & 0xFF:02X} {addr >> 8:02X}"


# defMON player SID-write-band signature (the $1022 play body): loads the
# V1/V2 pulse+freq operands and writes $D400..$D406, ending in a JMP. This
# is the same anchor sidid keys "DefMon" on. ``??`` tokens are wildcard bytes
# (per-tune relocated operands / immediate values); the SID write targets are
# derived from ``pysidtracker.registers`` rather than baked-in address literals.
_SIG_SPEC = " ".join(
    (
        "A2 ?? A9 ??",
        _sid_store("8E", 2),  # STX $D402
        _sid_store("8D", 3),  # STA $D403
        "A2 ?? A9 ??",
        _sid_store("8E", 0),  # STX $D400
        _sid_store("8D", 1),  # STA $D401
        "A2 ?? A0 ?? A9 ?? ?? ??",
        _sid_store("8E", 6),  # STX $D406
        _sid_store("8C", 5),  # STY $D405
        _sid_store("8D", 4),  # STA $D404
        "4C",  # JMP
    )
)
_SIG_PATTERN = CodePattern(_SIG_SPEC)
# Public masked-token tuple (``None`` == wildcard), derived from the same spec
# the CodePattern compiles, for corpus discovery / prefilter helpers.
SIGNATURE: tuple = tuple(None if t == "??" else int(t, 16) for t in _SIG_SPEC.split())

# Signature deltas (canonical player base $1000, play body $1022, data base $1800).
_SIGNATURE_FROM_PLAYER_BASE = 0x22
_DATA_BASE_FROM_SIGNATURE = 0x7DE


def find_signature(mem, start: int = 0, end: int = 0x10000) -> int:
    """Return the address of defMON's replay signature in ``mem``, or ``-1``.

    ``mem`` is a 64 KiB image (e.g. :attr:`SidImage.mem`). The masked scan is
    delegated to :func:`pysidtracker.find_code_first`, which now accepts the raw
    buffer directly, over the shared :class:`~pysidtracker.CodePattern`.
    """
    match = find_code_first(mem, _SIG_PATTERN, start=start, end=end)
    return match.addr if match is not None else -1


def prg_replay(raw: bytes) -> Optional[Tuple[int, int]]:
    """Detect a defMON pack-save export PRG and recover its player base.

    Both the ``$1000`` raw export and the ``$0801`` executable (BASIC stub +
    host boot ahead of the same replay) carry the player a fixed distance
    below its signature, so the base follows from the signature site alone.

    Returns:
        ``(load, player_base)`` or ``None`` when ``raw`` carries no defMON
        player with a consistent data base (e.g. a ``$1800`` workfile).
    """
    if raw[:4] in (b"PSID", b"RSID") or len(raw) < 4:
        return None
    load, body = parse_prg(raw)
    match = find_code_first(body, _SIG_PATTERN, start=0, end=len(body))
    if match is None:
        return None
    sig = match.addr
    if sig < _SIGNATURE_FROM_PLAYER_BASE:
        return None
    if sig + _DATA_BASE_FROM_SIGNATURE >= len(body):
        return None
    return load, load + sig - _SIGNATURE_FROM_PLAYER_BASE


def _reconstruct(mem, data_base: int) -> bytearray:
    """Re-expand a packed replay's data into the editor-layout snapshot."""
    delta = data_base - LOAD_ADDRESS
    snap = bytearray(SNAPSHOT_SIZE)

    def src(addr: int) -> int:  # canonical editor addr -> relocated read
        a = addr + delta
        return mem[a] if 0 <= a < 0x10000 else 0

    def absrd(addr: int) -> int:  # absolute (already relocated) read
        return mem[addr] if 0 <= addr < 0x10000 else 0

    # Low editor regions ($1800..$1EFF: pointer arrays, arrangers, DL)
    # carry over verbatim; the sidTAB pointer arrays are rewritten below.
    for addr in range(LOAD_ADDRESS, PATTERN_BANK):
        snap[addr - LOAD_ADDRESS] = src(addr)

    # Pattern bodies: $1A00/$1A80 holds each body's absolute address.
    for n in range(128):
        start = src(0x1A00 + n) | (src(0x1A80 + n) << 8)
        store_pattern(snap, absrd, n, start)

    # sidTAB rows: $1800/$1900 pointer pair. Non-zero hi => absolute address
    # of the row's compacted body; re-expand into fixed $5F00 slots and mark
    # the row active ($11) so DefmonSong.unpacked_snapshot() recomputes the
    # runtime pointer as $5F00 + Y*15. Zero hi => JP source: keep the target
    # row index (lo) and the zero marker.
    for y in range(256):
        hi = src(0x1900 + y)
        lo = src(0x1800 + y)
        off_lo = 0x1800 + y - LOAD_ADDRESS
        off_hi = 0x1900 + y - LOAD_ADDRESS
        if hi == 0:
            snap[off_hi] = 0
            snap[off_lo] = lo
            continue
        row_addr = lo | (hi << 8)
        relocate_sidtab_row(snap, absrd, y, row_addr)
        snap[off_hi] = 0x11
        snap[off_lo] = 0

    # CIA-2 timer rate ($715A..$715C) if the replay keeps it at the editor
    # offset; otherwise DefmonPlayer falls back to the PAL default.
    for addr in (0x715A, 0x715B, 0x715C):
        snap[addr - LOAD_ADDRESS] = src(addr)

    return snap


def _referenced_patterns(snap: bytearray) -> List[int]:
    seen = set()
    for bank in (0x1B00, 0x1C00, 0x1D00):
        base = bank - LOAD_ADDRESS
        for y in range(256):
            b = snap[base + y]
            if b not in (0, 0xFF):
                seen.add(b & 0x7F)
    return sorted(seen)


def _has_pattern_data(snap: bytearray) -> bool:
    """True if any arranger-referenced pattern has a non-empty body."""
    for pat in _referenced_patterns(snap):
        base = PATTERN_BANK + pat * PATTERN_STRIDE - LOAD_ADDRESS
        if any(snap[base : base + PATTERN_STRIDE]):
            return True
    return False


def depack_replay(image: SidImage, signature: Optional[int] = None) -> Optional[bytes]:
    """Reconstruct the editor-layout snapshot from a defMON ``.sid`` replay.

    Returns the 22887-byte ``$1800..$7166`` runtime RAM image, or ``None``
    if ``image`` is not a recognised defMON replay or its data layout cannot
    be mapped to the editor layout (a small number of HVSC tunes use a newer
    packer variant with a compact / indirect data layout).

    ``signature`` is the player-signature address when the caller already
    located it (:func:`find_signature`); otherwise it is scanned for here.
    """
    if signature is None:
        signature = find_signature(image.mem)
    if signature < 0:
        return None
    data_base = signature + _DATA_BASE_FROM_SIGNATURE
    snap = _reconstruct(image.mem, data_base)
    if not _has_pattern_data(snap):
        return None
    return bytes(snap)
