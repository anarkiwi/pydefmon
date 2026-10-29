"""Run defMON's own ``$D6C9`` worktune LOAD as a fidelity oracle.

A thin wrapper over :mod:`pysidtracker.editor` that runs ``decoder_load_setup``
(``$CEB2``) on the decrunched editor and returns the post-LOAD image at
``$1800..$7166`` — a byte reference pydefmon's reads are checked against.
"""

from __future__ import annotations

from pysidtracker.editor import editor_replay as _editor_replay, run_editor

from pydefmon._packer import (
    DECODE_ENTRY,
    INIT_ADDRESS,
    PLAY_ADDRESS,
    PRG_LOAD,
    REPLAY_BASE,
)

#: One past the last editor song byte ($7166).
SNAPSHOT_TOP_EXCL = 0x7167


def load_worktune(editor_image: bytes, worktune_prg: bytes) -> bytes:
    """defMON's own LOAD of ``worktune_prg``; returns the post-LOAD image.

    ``worktune_prg`` is the raw ``.prg`` (load-address header + body); its
    body is placed at ``$1800`` and ``$CEB2`` runs. Returns ``$1800..$7166``.
    """
    body = worktune_prg[2:]
    if not body:
        raise ValueError("worktune .prg carries no body")
    mem = run_editor(editor_image, body, PRG_LOAD, (DECODE_ENTRY,))
    return bytes(mem[PRG_LOAD:SNAPSHOT_TOP_EXCL])


def editor_replay(editor_image: bytes, worktune_prg: bytes) -> bytes:
    """A PSID of the editor's own player over its LOAD of ``worktune_prg``.

    The editor plays the fixed-stride ``$1800`` song with its live player at
    ``$1000`` (init ``$1000``, play ``$1003``), so this is the worktune as the
    composer hears it, with no packer in between.
    """
    return _editor_replay(
        editor_image,
        worktune_prg[2:],
        PRG_LOAD,
        (DECODE_ENTRY,),
        (REPLAY_BASE, SNAPSHOT_TOP_EXCL),
        INIT_ADDRESS,
        PLAY_ADDRESS,
        flags=0x20,
    )
