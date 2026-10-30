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
