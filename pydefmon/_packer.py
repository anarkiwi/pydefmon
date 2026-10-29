"""Pack a ``.prg`` worktune into defMON's own `$1000` replay.

Runs a user-supplied decrunched defMON editor image
(:func:`pysidtracker.editor.run_editor`) through its SAVE pipeline (worktune LOAD, ``$CEB2`` decode, ``$D02C`` pack),
yielding defMON's own packed bytes; entry points are stable across
V2014xxxx..V20201008 (source: ``undefmon/defmon.asm``).
"""

from __future__ import annotations

from pysidtracker.editor import run_editor

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
_RAM_SIZE = 0x10000


def pack(editor_image: bytes, worktune_prg: bytes) -> tuple[bytes, int]:
    """Run defMON's own SAVE pipeline to pack a worktune PRG.

    Takes a 65536-byte decrunched editor RAM image and the raw worktune
    PRG; returns ``(image, end)`` — the packed replay bytes
    ``mem[$1000..end)`` and the exclusive end from ``song_end_pointer``.
    """
    if len(worktune_prg) < 4:
        raise ValueError("worktune prg is too short to carry a load address + body")
    mem = run_editor(editor_image, worktune_prg, PRG_LOAD, (DECODE_ENTRY, PACK_ENTRY))
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
