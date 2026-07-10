"""SID register write logs for defMON playback.

Thin defMON-specific wrapper over :mod:`pysidtracker.reglog`, the shared
py* register-log surface (``RegWrite`` / ``read_reglog`` / ``write_reglog``
/ ``frame_writes``). A register log flattens the player's per-frame output
to timed chip writes: one :class:`~pysidtracker.reglog.RegWrite` per SID
register write, with an absolute clock in C64 CPU cycles. Downstream tooling
(including deplayroutine's generic-interpreter validator harness) consumes a
defMON tune's output exactly as it consumes any other player's.

The register index ``reg`` is the SID register OFFSET (``0..$18``) relative to
``$D400``, NOT the absolute address. :meth:`DefmonPlayer.play_frame` yields
ABSOLUTE addresses (``$D400..$D418``); :func:`iter_register_writes` rebases
them to offsets via :func:`~pysidtracker.reglog.frame_writes`
(``sid_reg_base=$D400``), keeping the tune's play-routine cadence
(:func:`pysidtracker.cadence.playroutine_cadence`).
"""

from __future__ import annotations

from typing import Iterator

from pysidtracker.reglog import (  # re-exported for the shared py* surface
    DEFAULT_WRITE_SPACING,
    SID_REG_COUNT,
    RegWrite,
    frame_writes,
    read_reglog,
    write_reglog,
)

from pydefmon.defmon import DefmonSong
from pydefmon.defmon_player import SID_REG_BASE, DefmonPlayer

__all__ = [
    "DEFAULT_MAX_FRAMES",
    "DEFAULT_WRITE_SPACING",
    "RegWrite",
    "SID_REGISTERS",
    "iter_register_writes",
    "read_reglog",
    "write_reglog",
]

# Default playback bound: one minute at defMON's ~50 Hz nominal rate.
# The real per-tune rate comes from the play-routine cadence
# (``DefmonPlayer.cycles_per_frame``); ``max_frames`` only bounds the
# (otherwise looping) log.
DEFAULT_MAX_FRAMES = 50 * 60

# SID register file size ($D400..$D418): 25 registers.
SID_REGISTERS = SID_REG_COUNT


def iter_register_writes(
    song: DefmonSong,
    max_frames: int = DEFAULT_MAX_FRAMES,
    cycles_per_frame: "int | None" = None,
    write_spacing: int = DEFAULT_WRITE_SPACING,
) -> Iterator[RegWrite]:
    """Yield :class:`RegWrite` for ``song``, frame by frame.

    Drives a fresh :class:`DefmonPlayer` over ``song`` for ``max_frames``
    main player ticks (the player loops forever, so ``max_frames`` bounds
    the log).  ``cycles_per_frame`` defaults to the song's own play-routine
    cadence (``DefmonPlayer.cycles_per_frame`` -- from
    :func:`pysidtracker.cadence.playroutine_cadence`); pass an explicit value
    to frame the log on a different cadence (e.g. the PAL VBI period a PSID
    export plays at).  Writes within a frame are spaced ``write_spacing``
    cycles from the frame boundary; frames are ``cycles_per_frame`` apart.

    The yielded ``reg`` is the SID register offset ``0..$18`` (rebased from
    :meth:`DefmonPlayer.play_frame`'s absolute ``$D400..$D418`` by
    :func:`~pysidtracker.reglog.frame_writes`).
    """
    player = DefmonPlayer(song)
    if cycles_per_frame is None:
        cycles_per_frame = player.cycles_per_frame
    per_frame = (player.play_frame() for _ in range(max_frames))
    yield from frame_writes(
        per_frame,
        cycles_per_frame=cycles_per_frame,
        write_spacing=write_spacing,
        sid_reg_base=SID_REG_BASE,
    )
