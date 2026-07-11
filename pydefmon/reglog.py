"""SID register write logs for defMON playback.

Thin defMON binding of :mod:`pysidtracker.reglog`, the shared ``py*`` register-
log surface (``RegWrite`` / ``read_reglog`` / ``write_reglog``). A register log
flattens a player's per-frame output to timed chip writes: one
:class:`~pysidtracker.reglog.RegWrite` per SID register write, with an absolute
clock in C64 CPU cycles and ``reg`` the register OFFSET ``0..$18`` relative to
``$D400``. Downstream tooling consumes a defMON tune's output exactly as it
consumes any other ``py*`` player's.

:func:`iter_register_writes` drives a :class:`~pydefmon.DefmonPlayer` through the
base :func:`~pysidtracker.reglog.register_writes_from_player`, which emits the
post-init register baseline at clock 0 then each frame's changed registers at the
tune's play-routine cadence (:attr:`DefmonPlayer.cycles_per_frame`).
"""

from __future__ import annotations

from typing import Iterator

from pysidtracker.reglog import (  # re-exported for the shared py* surface
    DEFAULT_WRITE_SPACING,
    SID_REG_COUNT,
    RegWrite,
    read_reglog,
    register_writes_from_player,
    write_reglog,
)

from pydefmon.defmon_player import DefmonPlayer, PlayerSource

__all__ = [
    "DEFAULT_MAX_FRAMES",
    "DEFAULT_WRITE_SPACING",
    "RegWrite",
    "SID_REGISTERS",
    "iter_register_writes",
    "read_reglog",
    "write_reglog",
]

# Default playback bound: one minute at defMON's ~50 Hz nominal rate. The real
# per-tune rate comes from the play-routine cadence; ``max_frames`` only bounds
# the (otherwise unbounded) log.
DEFAULT_MAX_FRAMES = 50 * 60

# SID register file size ($D400..$D418): 25 registers.
SID_REGISTERS = SID_REG_COUNT


def iter_register_writes(
    source: PlayerSource,
    max_frames: int = DEFAULT_MAX_FRAMES,
    cycles_per_frame: "int | None" = None,
    write_spacing: int = DEFAULT_WRITE_SPACING,
) -> Iterator[RegWrite]:
    """Yield :class:`RegWrite` for a defMON replay ``source``, frame by frame.

    Drives a fresh :class:`~pydefmon.DefmonPlayer` for ``max_frames`` play calls.
    ``cycles_per_frame`` defaults to the tune's own play-routine cadence
    (:attr:`DefmonPlayer.cycles_per_frame`); pass an explicit value to frame the
    log on a different cadence. Writes within a frame are ``write_spacing``
    cycles apart; frames are ``cycles_per_frame`` apart. The yielded ``reg`` is
    the SID register offset ``0..$18``.
    """
    player = DefmonPlayer(source)
    if cycles_per_frame is None:
        cycles_per_frame = player.cycles_per_frame
    yield from register_writes_from_player(
        player, max_frames, cycles_per_frame, write_spacing
    )
