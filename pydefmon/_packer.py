"""Pack a ``.prg`` worktune into defMON's own `$1000` replay.

Runs a user-supplied decrunched defMON editor image on a 6502 model of
its SAVE pipeline (worktune LOAD, ``$CEB2`` decode, ``$D02C`` pack),
yielding defMON's own packed bytes; entry points are stable across
V2014xxxx..V20201008 (source: ``undefmon/defmon.asm``).
"""

from __future__ import annotations

from pysidtracker import SidImage
from pysidtracker.emu import run_to_rts, set_iomap
from pysidtracker.header import write_psid
from pysidtracker.oracle import SID_VOLUME, boot_flat

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
#: The C64 memory-bank port.
BANK_PORT = 0x01
_MAX_CALL_CYCLES = 30_000_000
#: PSID flags selecting the 6581 (bits 4-5 = 1), the packer's SID model.
_SID_6581 = 0x10


def _editor_boot_image(mem: bytearray, entry: int) -> SidImage:
    """``mem`` (the 64 KiB editor RAM) as a flat ``SidImage`` whose ``init`` is ``entry``.

    The header only has to carry the init address and a 6581 (the packer's SID
    model); the tune image is the whole of RAM, loaded at ``$0000``.
    """
    stub = write_psid(
        load=PRG_LOAD, init=entry, play=entry, image=bytes(2), flags=_SID_6581
    )
    return SidImage(
        mem, 0, _RAM_SIZE, SidImage.from_bytes(stub).header, bytes(mem), b""
    )


def place_and_run(editor_image: bytes, payload: bytes, *entry_points: int) -> bytearray:
    """Place ``payload`` at ``$1800``, set the LOAD-end word, run defMON entries.

    Boots pysidtracker's flat 6502 (NMOS illegal opcodes) on a copy of the editor
    image with :func:`~pysidtracker.oracle.boot_flat` -- the first entry point runs
    as its ``init`` -- then runs each further ``entry_point`` in order with
    :func:`~pysidtracker.emu.run_to_rts`, and returns the (mutated) image so the
    caller can read defMON's output words.
    """
    if len(editor_image) != _RAM_SIZE:
        raise ValueError(
            "editor image must be 65536 bytes (decrunched defMON editor RAM), "
            f"got {len(editor_image)}"
        )
    if not payload:
        raise ValueError("payload is empty")
    if not entry_points:
        return bytearray(editor_image)
    mem = bytearray(editor_image)
    bank, volume = mem[BANK_PORT], mem[SID_VOLUME]

    def place(mpu, subject) -> None:
        # boot_flat boots like the PSID driver (ROM banked in, volume up); defMON's own
        # editor state ($01, $D418) is what the SAVE pipeline expects, so put it back.
        set_iomap(mpu, bank)
        subject[SID_VOLUME] = volume
        subject[PRG_LOAD : PRG_LOAD + len(payload)] = payload
        end = PRG_LOAD + len(payload)
        subject[LOAD_END_LO] = end & 0xFF
        subject[LOAD_END_HI] = end >> 8

    subject, mpu, bus, _play = boot_flat(
        _editor_boot_image(mem, entry_points[0]),
        max_cycles=_MAX_CALL_CYCLES,
        setup=place,
    )
    set_iomap(mpu, bank)  # boot_flat banked in the play address's ROM
    for entry in entry_points[1:]:
        run_to_rts(mpu, bus, entry, 0, _MAX_CALL_CYCLES)
    return subject


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
