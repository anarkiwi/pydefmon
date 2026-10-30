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


def sidrow_binding(mem, base: int, voice: int) -> dict:
    """The ``defmon_sidrow.play`` layout of the player at ``base`` in ``mem``, for ``voice``."""
    binding = {name: base + off for name, off in SIDROW_OFFSETS.items()}
    cp = SIDROW_CP_OPERAND + base
    binding["cp"] = mem[cp] | mem[cp + 1] << 8
    binding["ptr_lo"], binding["ptr_hi"] = ROW_POINTER
    binding["voice"] = voice
    return binding
