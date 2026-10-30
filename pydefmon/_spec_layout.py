"""Binding of the playspec fragments to a replay's code (private).

The player keeps most of its state in the operands of its own instructions, at
fixed offsets from the player base (the signature address less
``_SIGNATURE_FROM_PLAYER_BASE``).
"""

from __future__ import annotations

from pydefmon._sid_format import _SIGNATURE_FROM_PLAYER_BASE, find_signature

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
}

#: The ACID block: its first instruction and the one after its ``STA $D416``.
ACID_BLOCK = (0xB5, 0xD8)


def player_base(mem, start: int = 0, end: int = 0x10000) -> int:
    """The address of the replay player in ``mem``, from its signature."""
    return find_signature(mem, start, end) - _SIGNATURE_FROM_PLAYER_BASE


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
