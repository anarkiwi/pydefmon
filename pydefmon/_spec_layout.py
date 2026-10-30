"""Binding of the playspec fragments to a replay's code (private).

The player keeps most of its state in the operands of its own instructions, at
fixed offsets from the player base (the signature address less
``_SIGNATURE_FROM_PLAYER_BASE``).
"""

from __future__ import annotations

from pathlib import Path

from pysidtracker import CodePattern, find_code_all, find_code_first

from pydefmon._sid_format import (
    _SIG_SPEC,
    _SIGNATURE_FROM_PLAYER_BASE,
    find_signature,
)

SPECS = Path(__file__).resolve().parent / "specs"


def spec_text(name: str) -> str:
    """The source of the packaged playspec ``name`` (``defmon``, ``defmon_band``...)."""
    return (SPECS / f"{name}.play").read_text(encoding="utf-8")


SID_BASE = 0xD400

#: ``defmon_acid.play`` layout: offsets of the cutoff slide's operands from the player base.
ACID_OFFSETS = {
    "acc_lo": 0xB6,
    "step_lo": 0xB9,
    "acc_hi": 0xBE,
    "step_hi": 0xC0,
    "opcode": 0xBF,
    "extra": 0xCA,
    "floor": 0xCE,
    "shift": 0xD4,
}

#: The ACID block: its first instruction and the one after its ``STA $D416``.
ACID_BLOCK = (0xB5, 0xD8)


def player_base(mem, start: int = 0, end: int = 0x10000) -> int:
    """The address of the replay player in ``mem``, from its signature."""
    return find_signature(mem, start, end) - _SIGNATURE_FROM_PLAYER_BASE


#: The band's signature with the SID's page left open: the second copy of a stereo
#: tune's player drives another chip.
_COPY_PATTERN = CodePattern(_SIG_SPEC.replace(" D4", " ??"))


def player_bases(mem) -> list:
    """The addresses of every copy of the replay player in ``mem``: a stereo tune plays
    one copy per chip."""
    found = find_code_all(bytes(mem), _COPY_PATTERN)
    return [match.addr - _SIGNATURE_FROM_PLAYER_BASE for match in found]


def entry_base(mem, entry: int) -> int:
    """The base of the copy of the player that ``entry`` enters.

    Raises:
        ValueError: ``entry`` is not within a copy of the player.
    """
    for base in player_bases(mem):
        if base - 3 <= entry < base + 0x900:
            return base
    raise ValueError(f"${entry:04x} is not in a copy of the player")


def play_entry(mem, base: int) -> int:
    """The address of the player's play entry: the ``JMP`` to its SID write band.

    A tune's own play address may wrap it, and its distance from ``base`` varies.

    Raises:
        ValueError: no such ``JMP`` is next to the player's start.
    """
    band = base + BAND_BLOCK[0]
    jump = bytes((0x4C, band & 0xFF, band >> 8))
    for at in range(base - 3, base + 0x10):
        if bytes(mem[at : at + 3]) == jump:
            return at
    raise ValueError(f"no play entry jumps to the band at ${band:04x}")


#: The entry at the player's start plus six: it patches an ``RTS`` over the walker's first
#: byte, calls the band (so the cutoff slide runs and the walker and readers do not) and
#: jumps to the sidTAB cascade.
REDUCED_ENTRY = 6
_REDUCED_STUB = CodePattern("AD ?? ?? 48 A9 60 8D ?? ?? 20 ?? ?? 68 8D ?? ?? 4C ?? ??")


def reduced_entry(mem, base: int, walker: int, cascade: int) -> "int | None":
    """The address of the reduced entry, or ``None`` if the player has none."""
    at = base + REDUCED_ENTRY
    found = find_code_first(bytes(mem), _REDUCED_STUB, start=at, end=at + 19)
    if found is None or found.addr != at:
        return None
    if _word(mem, at + 1) != walker or _word(mem, at + 17) != cascade:
        return None
    return at


def acid_binding(base: int) -> dict:
    """The ``defmon_acid.play`` layout of the player at ``base``."""
    binding = {name: base + off for name, off in ACID_OFFSETS.items()}
    binding["sid"] = SID_BASE
    return binding


#: ``defmon_pwsweep.play`` layout: offsets of voice 0's sweep state from the player base.
PWSWEEP_OFFSETS = {"depth": 0x1E, "pw_lo": 0x23, "pw_hi": 0x25}

#: The sweep of every voice: its first instruction and the voice loop's ``TXA``.
PWSWEEP_BLOCK = (0x495, 0x4E3)


def pwsweep_binding(base: int) -> dict:
    """The ``defmon_pwsweep.play`` layout of the player at ``base``."""
    return {name: base + off for name, off in PWSWEEP_OFFSETS.items()}


#: ``defmon_sidrow.play`` layout: offsets from the player base; per-voice cells
#: are those of voice 0, the next voices follow ``VOICE_STRIDE`` bytes apart.
SIDROW_OFFSETS = {
    "wgh": 0x3B,
    "wgl": 0x3D,
    "ad": 0x39,
    "sr": 0x37,
    "note": 0x37F,
    "note_base": 0x2ED,
    "af": 0x1B,
    "pw_lo": 0x23,
    "pw_hi": 0x25,
    "ps": 0x1E,
    "vbit": 0x20,
    "vmask": 0x21,
    "res": 0xAA,
    "fv": 0xAF,
    "acc_lo": 0xB6,
    "acc_hi": 0xBE,
    "step_lo": 0xB9,
    "step_hi": 0xC0,
    "op_lo": 0xB8,
    "op_hi": 0xBF,
}

#: The row interpreter's entry (``STA $FB``), and the operand of its CP store, which
#: relocated replays leave at the address it had in the unrelocated player.
SIDROW_ENTRY = 0x6B0
SIDROW_CP_OPERAND = 0x765
SIDROW_SCRATCH_OPERAND = 0x6BA
ROW_POINTER = (0xFB, 0xFC)
VOICE_STRIDE = 0x31


def sidrow_scratch(mem, base: int) -> int:
    """The zero-page byte the row interpreter keeps its shifted bitmap in."""
    return mem[base + SIDROW_SCRATCH_OPERAND]


def sidrow_binding(mem, base: int) -> dict:
    """The ``defmon_sidrow.play`` layout of the player at ``base`` in ``mem``."""
    binding = {name: base + off for name, off in SIDROW_OFFSETS.items()}
    cp = SIDROW_CP_OPERAND + base
    binding["cp"] = mem[cp] | mem[cp + 1] << 8
    binding["ptr_lo"], binding["ptr_hi"] = ROW_POINTER
    binding["scratch"] = sidrow_scratch(mem, base)
    return binding


#: ``defmon_cascade.play`` layout: offsets from the player base of block 0's counter
#: and row-index operands, and of the row tables.
CASCADE_OFFSETS = {
    "counter": 0x2E0,
    "rowidx": 0x2EF,
    "rows_lo": 0x800,
    "rows_hi": 0x900,
    "dl": 0xE00,
}

#: The six cascade blocks, first instruction to the first of the sweep loop.
CASCADE_BLOCKS = (0x2DF, 0x405)
RTS = 0x60


def cascade_binding(base: int) -> dict:
    """The ``defmon_cascade.play`` layout of the player at ``base``."""
    binding = {name: base + off for name, off in CASCADE_OFFSETS.items()}
    binding["ptr_hi"] = ROW_POINTER[1]
    return binding


#: ``defmon_pitch.play`` layout: offsets from the player base of voice 0's cells, the
#: note-pitch and slide-rate tables, and the operands that hold the constant-detune step.
PITCH_OFFSETS = {
    "mode": 0x1B,
    "acc_lo": 0x19,
    "acc_hi": 0x1A,
    "detune": 0x1F,
    "note": 0x37F,
    "freq_lo": 0x2D,
    "freq_hi": 0x2F,
    "pitch_lo": 0x59C,
    "pitch_hi": 0x638,
    "rate_lo": 0x578,
    "rate_hi": 0x614,
    "step_lo": 0x489,
    "step_hi": 0x491,
}

#: The pitch of one voice: its first instruction and the start of the pulse-width sweep.
PITCH_BLOCK = (0x407, 0x495)


def pitch_binding(base: int, voice: int) -> dict:
    """The ``defmon_pitch.play`` layout of the player at ``base``, for ``voice``."""
    binding = {name: base + off for name, off in PITCH_OFFSETS.items()}
    binding["voice"] = voice
    return binding


#: ``defmon_walker.play`` layout: offsets from the player base of the trigger and cursor
#: operands, the loop owner and count, the arranger and pattern-pointer tables, and the
#: operands that hold each voice's pattern pointer and step counter.
WALKER_OFFSETS = {
    "trigger": 0xD9,
    "cursor": 0xEB,
    "owner": 0x4EC,
    "count": 0x4ED,
    "v1": 0xB00,
    "v2": 0xC00,
    "v3": 0xD00,
    "ptr_lo": 0xA00,
    "ptr_hi": 0xA80,
    "p0_lo": 0x186,
    "p0_hi": 0x187,
    "p1_lo": 0x20E,
    "p1_hi": 0x20F,
    "p2_lo": 0x296,
    "p2_hi": 0x297,
    "c0": 0x14A,
    "c1": 0x1D2,
    "c2": 0x25A,
}

#: The walker: its first instruction (``LDA #trigger``) and the start of the voices.
WALKER_BLOCK = (0xD8, 0x147)


def walker_binding(base: int) -> dict:
    """The ``defmon_walker.play`` layout of the player at ``base``."""
    return {name: base + off for name, off in WALKER_OFFSETS.items()}


#: ``defmon_reader.play`` layout: offsets from the player base of voice 0's reader
#: operands; voice ``n`` is ``READER_STRIDE * n`` further on for the reader's own
#: operands and ``VOICE_STRIDE * n`` for the records it writes.
READER_OFFSETS = {
    "trigger": 0xD9,
    "counter": 0x14A,
    "ptr_lo": 0x186,
    "ptr_hi": 0x187,
    "copy1_lo": 0x16B,
    "copy1_hi": 0x16C,
    "copy2_lo": 0x194,
    "copy2_hi": 0x195,
    "copy3_lo": 0x1A2,
    "copy3_hi": 0x1A3,
    "flag": 0x1B5,
    "gate_a": 0x182,
    "gate_b": 0x190,
    "gate_n": 0x19E,
    "row_a": 0x2EF,
    "count_a": 0x2E0,
    "row_b": 0x382,
    "count_b": 0x373,
    "note_base": 0x2ED,
    "note": 0x37F,
    "acc_lo": 0x19,
    "acc_hi": 0x1A,
    "mode": 0x1B,
}

#: Voice 0's reader: its first instruction; the next voice follows ``READER_STRIDE`` on.
READER_START = 0x147
READER_STRIDE = 0x88


def reader_binding(base: int, voice: int) -> dict:
    """The ``defmon_reader.play`` layout of the player at ``base``, for ``voice``."""
    binding = {name: base + off for name, off in READER_OFFSETS.items()}
    binding["voice"] = voice
    return binding


#: ``defmon_band.play`` layout: offsets from the player base of voice 0's record fields
#: and of the routing and filter-mode operands the band ends with.
BAND_OFFSETS = {
    "pw_lo": 0x23,
    "pw_hi": 0x25,
    "freq_lo": 0x2D,
    "freq_hi": 0x2F,
    "sr": 0x37,
    "ad": 0x39,
    "wgh": 0x3B,
    "wgl": 0x3D,
    "res": 0xAA,
    "fv": 0xAF,
}

#: The band: its first instruction and the first of the cutoff slide that follows it.
BAND_BLOCK = (0x22, 0xB5)


def band_binding(base: int) -> dict:
    """The ``defmon_band.play`` layout of the player at ``base``."""
    binding = {name: base + off for name, off in BAND_OFFSETS.items()}
    binding["sid"] = SID_BASE
    return binding


#: The cutoff block's start, its end store, the reader's first instructions, and the row
#: interpreter's: the anchors from which the blocks whose size varies with the build are found.
_CUTOFF_START = 0xB5
_CUTOFF_STORE = CodePattern("8D 16 ??")
_UNCOUNTED_JUMPS = CodePattern("BE ?? ?? 10 ?? B9 ?? ?? A8 BE")
_READER_START = CodePattern("A2 00 A0 ?? 30 ?? CE")
_ROW_START = CodePattern("85 ?? A0 00 B1 ??")
_ROW_TR = CodePattern("18 7D ?? ??")
_ROW_CP = CodePattern("8D ?? ?? 8A 29 10")
_BAND_CONTROL = 0x3C
OR_OPCODE = 0x09

#: The cutoff block's length from its start to the end of its ``STA $D416``, by shape.
_CUTOFF_WITH_SHIFT = 0x23
_CUTOFF_WITH_FLOOR = 0x22
READER_SIZE = 0x88
CASCADE_SIZE = 6 * 0x31
PITCH_AFTER_CASCADE = 0x2


def _word(mem, at: int) -> int:
    return mem[at] | mem[at + 1] << 8


def _find(mem, pattern: CodePattern, start: int, end: int) -> int:
    """The address of the first ``pattern`` in ``mem`` from ``start`` to ``end``."""
    found = find_code_first(bytes(mem), pattern, start=start, end=end)
    if found is None:
        raise ValueError(f"no code like {pattern} between ${start:04x} and ${end:04x}")
    return found.addr


def _operands(mem, opcode: int, start: int, end: int) -> list:
    """The distinct absolute operands of the ``opcode`` instructions from ``start`` to
    ``end``, in order."""
    found: list = []
    for at in range(start, end - 2):
        if mem[at] == opcode and _word(mem, at + 1) not in found:
            found.append(_word(mem, at + 1))
    return found


#: The reader's gate branch skips the load alone (its ``INY`` runs for every field)
#: in the build that keeps each pattern step at four bytes.
_FIXED_EVENT_SKIP = 0x09


def play_entries(mem, base: int) -> set:
    """The addresses the player's routines are entered at: the ``JMP`` beside its start
    to the band when it has one, the band (which a ``JSR`` or ``JMP`` from the tune's own
    code reaches), and the reduced entry when the player has one."""
    band = base + BAND_BLOCK[0]
    entries = {band}
    try:
        entries.add(play_entry(mem, base))
    except ValueError:
        pass
    reduced = _reduced_entry(mem, base)
    if reduced is not None:
        entries.add(reduced)
    return entries


def _reduced_entry(mem, base: int) -> "int | None":
    walker = (
        _find(mem, _CUTOFF_STORE, base + _CUTOFF_START, base + _CUTOFF_START + 0x30) + 3
    )
    reader = _find(mem, _READER_START, walker, walker + 0x100)
    return reduced_entry(mem, base, walker, reader + 3 * READER_SIZE)


def defmon_binding(mem, base: int, entry: "int | None" = None) -> dict:
    """The ``defmon.play`` layout of the player at ``base`` in ``mem``.

    The blocks whose length varies with the build (the cutoff slide, the arranger walker)
    are found from their code; table addresses are read from the operands that use them.

    ``entry`` is where the routine was entered: the reduced entry binds ``reduced``.

    Raises:
        ValueError: a block is not where the player's code puts it.
    """
    cutoff = base + _CUTOFF_START
    store = _find(mem, _CUTOFF_STORE, cutoff, cutoff + 0x30)
    walker = store + 3
    length = walker - cutoff
    reader = _find(mem, _READER_START, walker, walker + 0x100)
    cascade = reader + 3 * READER_SIZE
    pitch = cascade + CASCADE_SIZE + PITCH_AFTER_CASCADE
    row = _find(mem, _ROW_START, base, base + 0x900)
    tr = _find(mem, _ROW_TR, row, row + 0x100)
    cp = _find(mem, _ROW_CP, row, row + 0x100)
    uncounted = (
        find_code_first(bytes(mem), _UNCOUNTED_JUMPS, start=walker, end=reader)
        is not None
    )
    pages = _operands(mem, 0xBE, walker, reader)
    patterns = _operands(mem, 0xBD, walker, reader)
    binding = {
        # the voice records and the band
        "pw_lo": base + 0x23,
        "pw_hi": base + 0x25,
        "freq_lo": base + 0x2D,
        "freq_hi": base + 0x2F,
        "sr": base + 0x37,
        "ad": base + 0x39,
        "wgh": base + 0x3B,
        "wgl": base + 0x3D,
        "slide_lo": base + 0x19,
        "slide_hi": base + 0x1A,
        "mode": base + 0x1B,
        "depth": base + 0x1E,
        "detune": base + 0x1F,
        "vbit": base + 0x20,
        "vmask": base + 0x21,
        "res": base + 0xAA,
        "fv": base + 0xAF,
        # the cutoff slide
        "cut_lo": cutoff + 1,
        "op_lo": cutoff + 3,
        "cut_step_lo": cutoff + 4,
        "cut_hi": cutoff + 9,
        "op_hi": cutoff + 0xA,
        "cut_step_hi": cutoff + 0xB,
        "extra": cutoff + (0x15 if length >= _CUTOFF_WITH_FLOOR else 0x14),
        "floor": cutoff + 0x19,
        "shift": cutoff + 0x1F,
        # the arranger walker
        "trigger": walker + 1,
        "cursor": walker + 0x13,
        "owner": _word(mem, walker + 0x1F) if not uncounted else walker,
        "count": _word(mem, walker + 0x27) if not uncounted else walker,
        "v1": pages[0],
        "v2": pages[1],
        "v3": pages[2],
        "pat_lo": patterns[0],
        "pat_hi": patterns[1],
        # the pattern readers
        "counter": reader + 3,
        "copy1_lo": reader + 0x24,
        "copy1_hi": reader + 0x25,
        "gate_a": reader + 0x3B,
        "ptr_lo": reader + 0x3F,
        "ptr_hi": reader + 0x40,
        "gate_b": reader + 0x49,
        "copy2_lo": reader + 0x4D,
        "copy2_hi": reader + 0x4E,
        "gate_n": reader + 0x57,
        "copy3_lo": reader + 0x5B,
        "copy3_hi": reader + 0x5C,
        "flag": reader + 0x6E,
        # the cascade
        "blk_counter": cascade + 1,
        "blk_rowidx": cascade + 0x10,
        "count_a": cascade + 1,
        "row_a": cascade + 0x10,
        "count_b": cascade + 1 + 3 * 0x31,
        "row_b": cascade + 0x10 + 3 * 0x31,
        "note_base": cascade + 0xE,
        "note": cascade + 0xA0,
        "rows_hi": _word(mem, cascade + 0x12),
        "dl": _word(mem, cascade + 0x20),
        "rows_lo": _word(mem, cascade + 0x26),
        # the pitch
        "rate_lo": _word(mem, pitch + 0x21),
        "rate_hi": _word(mem, pitch + 0x2B),
        "pitch_lo": _word(mem, pitch + 0x50),
        "pitch_hi": _word(mem, pitch + 0x5A),
        "step_lo": pitch + 0x82,
        "step_hi": pitch + 0x8A,
        # the row interpreter
        "cp": _word(mem, cp + 1),
        "rowptr_lo": mem[row + 1],
        "rowptr_hi": mem[row + 1] + 1,
        "scratch": mem[row + 0xA],
        "sid": _word(mem, base + BAND_BLOCK[0] + 5) - 2,
        # the build
        "uncounted": int(uncounted),
        "plain_tr": int(
            bytes(mem[tr + 4 : tr + 6]) != b"\x29\x7f" and mem[tr + 4] == 0x9D
        ),
        "or_ctrl": int(mem[base + _BAND_CONTROL] == OR_OPCODE),
        "floored": int(length >= _CUTOFF_WITH_FLOOR),
        "shifts": int(length >= _CUTOFF_WITH_SHIFT),
        "fixed": int(mem[reader + 0x3B + 2] == _FIXED_EVENT_SKIP),
        "reduced": int(
            entry is not None and entry == reduced_entry(mem, base, walker, cascade)
        ),
    }
    return binding
