"""Pack a ``.prg`` worktune into defMON's own `$1000` replay.

Runs a user-supplied decrunched defMON editor image on a 6502 model of
its SAVE pipeline (worktune LOAD, ``$CEB2`` decode, ``$D02C`` pack),
yielding defMON's own packed bytes; entry points are stable across
V2014xxxx..V20201008 (source: ``undefmon/defmon.asm``).
"""

from __future__ import annotations

from pysidtracker.emu import run_to_rts, wire_mpu

from pydefmon._layout import LOAD_ADDRESS
from pydefmon._sid_format import find_signature

#: Replay base: the packed image starts at the player ($1000..$17FF).
REPLAY_BASE = 0x1000
INIT_ADDRESS = REPLAY_BASE
PLAY_ADDRESS = 0x1003
#: defMON editor workfile load address.
PRG_LOAD = LOAD_ADDRESS
DECODE_ENTRY = 0xCEB2  # decoder_load_setup
PACK_ENTRY = 0xD02C  # save_encode_orchestrator
#: Packed-image end (exclusive), set by the pack routine.
SONG_END_LO = 0xCE7D
SONG_END_HI = 0xCE7E
#: Load end pointer as the kernal leaves it after LOAD.
LOAD_END_LO = 0x00AE
LOAD_END_HI = 0x00AF
_RAM_SIZE = 0x10000
_MAX_CALL_CYCLES = 30_000_000


def place_and_run(editor_image: bytes, payload: bytes, *entry_points: int) -> bytearray:
    """Place ``payload`` at ``$1800``, set the LOAD-end word, run defMON entries.

    Wires pysidtracker's 6502 (NMOS illegal opcodes), runs each ``entry_point``
    in order on a copy of the editor image, and returns the (mutated) image so
    the caller can read defMON's output words.
    """
    if len(editor_image) != _RAM_SIZE:
        raise ValueError(
            "editor image must be 65536 bytes (decrunched defMON editor RAM), "
            f"got {len(editor_image)}"
        )
    if not payload:
        raise ValueError("payload is empty")
    mem = bytearray(editor_image)
    mem[PRG_LOAD : PRG_LOAD + len(payload)] = payload
    end = PRG_LOAD + len(payload)
    mem[LOAD_END_LO] = end & 0xFF
    mem[LOAD_END_HI] = end >> 8
    mpu, _obs = wire_mpu(mem, illegal_opcodes=True, sid_model="6581")
    for entry in entry_points:
        run_to_rts(mpu, mem, entry, 0, _MAX_CALL_CYCLES)
    return mem


def pack(editor_image: bytes, worktune_prg: bytes) -> tuple[bytes, int]:
    """Run defMON's own SAVE pipeline to pack a worktune PRG.

    Takes a 65536-byte decrunched editor RAM image and the raw worktune
    PRG; returns ``(image, end)`` — the packed replay bytes
    ``mem[$1000..end)`` and the exclusive end from ``song_end_pointer``.
    """
    if len(worktune_prg) < 4:
        raise ValueError("worktune prg is too short to carry a load address + body")
    mem = place_and_run(editor_image, worktune_prg, DECODE_ENTRY, PACK_ENTRY)
    song_end = mem[SONG_END_LO] | (mem[SONG_END_HI] << 8)
    if not (REPLAY_BASE < song_end < _RAM_SIZE):
        raise RuntimeError(
            f"defMON packer produced an implausible image end ${song_end:04X}"
        )
    image = bytes(mem[REPLAY_BASE:song_end])
    if find_signature(image) < 0:
        raise RuntimeError("defMON packer output has no player signature")
    return image, song_end


def to_prg(image: bytes) -> bytes:
    """Wrap a packed replay image as a loadable ``.prg`` (``$1000``)."""
    return (INIT_ADDRESS).to_bytes(2, "little") + image
