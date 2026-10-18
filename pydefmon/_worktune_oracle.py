"""Run defMON's own ``$D6C9`` worktune LOAD as a fidelity oracle.

Reusable wrapper over the decrunched editor's ``$CEB2`` decode on
pysidtracker's 6502; mirrors ``pydefmon._packer`` (entry points stable
across V2014xxxx..V20201008, see ``undefmon/defmon.asm``).
"""

from __future__ import annotations

from pysidtracker.emu import run_to_rts, wire_mpu

_RAM_SIZE = 0x10000
#: defMON editor workfile load address.
PRG_LOAD = 0x1800
#: One past the last editor song byte ($7166).
SNAPSHOT_TOP_EXCL = 0x7167
#: Kernal load-end word left by LOAD.
LOAD_END_LO = 0x00AE
LOAD_END_HI = 0x00AF
#: ``decoder_load_setup`` — the worktune decode entry point.
DECODE_ENTRY = 0xCEB2
_MAX_CALL_CYCLES = 30_000_000


def load_worktune(editor_image: bytes, worktune_prg: bytes) -> bytes:
    """defMON's own LOAD of ``worktune_prg``; returns the post-LOAD image.

    ``editor_image`` is a 65536-byte decrunched defMON editor RAM image;
    ``worktune_prg`` is the raw ``.prg`` (load-address header + body).
    Returns the editor song image ``$1800..$7166`` after ``$CEB2`` runs.
    """
    if len(editor_image) != _RAM_SIZE:
        raise ValueError(
            "editor image must be 65536 bytes (decrunched defMON editor RAM), "
            f"got {len(editor_image)}"
        )
    body = worktune_prg[2:]
    if not body:
        raise ValueError("worktune .prg carries no body")
    mem = bytearray(editor_image)
    mem[PRG_LOAD : PRG_LOAD + len(body)] = body
    end = PRG_LOAD + len(body)
    mem[LOAD_END_LO] = end & 0xFF
    mem[LOAD_END_HI] = end >> 8

    mpu, mm = wire_mpu(mem, illegal_opcodes=True, sid_model="6581")
    run_to_rts(mpu, mm, DECODE_ENTRY, 0, _MAX_CALL_CYCLES)
    return bytes(mem[PRG_LOAD:SNAPSHOT_TOP_EXCL])
