"""SID register write logs for defMON playback.

A register log flattens the player's per-frame output to timed chip
writes: one :class:`RegWrite` per SID register write, with an absolute
clock in C64 CPU cycles.  This mirrors the shared py* register-log
convention (``pygoattracker.reglog`` / ``pymusicassembler.reglog``) so
the same downstream tooling -- including deplayroutine's generic
interpreter validator harness -- can consume a defMON tune's output the
same way it consumes any other player's.

The register index ``reg`` is the SID register OFFSET (``0..$18``)
relative to ``$D400``, NOT the absolute address: this matches the
``pygoattracker`` / ``pymusicassembler`` convention (and the
deplayroutine ``grid_from_writes`` framing, which indexes the 25-entry
register file directly by ``reg``).  :meth:`DefmonPlayer.play_frame`
yields ABSOLUTE addresses (``$D400..$D418``); :func:`iter_register_writes`
rebases them to offsets here.

Logs serialize to plain text -- one ``clock reg val`` triple per line
(decimal, space separated, ``#`` comments allowed) -- so they load
directly into pandas or any line-based tooling.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import IO, Iterable, Iterator, NamedTuple

from pydefmon.defmon import DefmonSong
from pydefmon.defmon_player import SID_REG_BASE, DefmonPlayer

# Default playback bound: one minute at defMON's ~50 Hz nominal rate.
# The real per-tune rate comes from the song's CIA-2 timer + sub-frame
# count (``DefmonPlayer.cycles_per_frame``); ``max_frames`` only bounds
# the (otherwise looping) log.
DEFAULT_MAX_FRAMES = 50 * 60

# Cycles between consecutive writes within one frame, approximating the
# store instructions of the 6502 playroutine.  The exact spacing does
# not matter for per-frame grid framing (all writes of a frame land in
# the same frame bucket), but a small non-zero spacing keeps the log a
# faithful "one timed write per store" stream.
DEFAULT_WRITE_SPACING = 16

# SID register file size ($D400..$D418): 25 registers.
SID_REGISTERS = 0x19

REGLOG_HEADER = "# pydefmon register log: clock reg val"


class RegWrite(NamedTuple):
    """One SID register write at an absolute CPU clock (in cycles).

    ``reg`` is the register OFFSET ``0..$18`` relative to ``$D400`` (the
    shared py* register-log convention), ``val`` the byte written.
    """

    clock: int
    reg: int
    val: int


def iter_register_writes(
    song: DefmonSong,
    max_frames: int = DEFAULT_MAX_FRAMES,
    cycles_per_frame: "int | None" = None,
    write_spacing: int = DEFAULT_WRITE_SPACING,
) -> Iterator[RegWrite]:
    """Yield :class:`RegWrite` for ``song``, frame by frame.

    Drives a fresh :class:`DefmonPlayer` over ``song`` for ``max_frames``
    main player ticks (the player loops forever, so ``max_frames`` bounds
    the log).  ``cycles_per_frame`` defaults to the song's own per-tune
    player-IRQ interval (``DefmonPlayer.cycles_per_frame`` -- derived from
    the CIA-2 timer reload + sub-frame count); pass an explicit value to
    frame the log on a different cadence (e.g. the PAL VBI period a PSID
    export plays at).  Writes within a frame are spaced ``write_spacing``
    cycles from the frame boundary; frames are ``cycles_per_frame`` apart.

    The yielded ``reg`` is the SID register offset ``0..$18`` (rebased
    from :meth:`DefmonPlayer.play_frame`'s absolute ``$D400..$D418``).
    """
    player = DefmonPlayer(song)
    if cycles_per_frame is None:
        cycles_per_frame = player.cycles_per_frame
    for frame in range(max_frames):
        writes = player.play_frame()
        clock = frame * cycles_per_frame
        for offset, (reg, val) in enumerate(writes):
            yield RegWrite(clock + offset * write_spacing, reg - SID_REG_BASE, val)


def write_reglog(writes: Iterable[RegWrite], dst, header: bool = True) -> None:
    """Write a register log to a path or text file-like object."""

    def _dump(out: IO[str]) -> None:
        if header:
            print(REGLOG_HEADER, file=out)
        for write in writes:
            print(f"{write.clock} {write.reg} {write.val}", file=out)

    if isinstance(dst, (str, Path)):
        with open(dst, "w", encoding="utf-8") as out:
            _dump(out)
        return
    _dump(dst)


def read_reglog(src) -> "list[RegWrite]":
    """Read a register log from a path or text file-like object."""
    if isinstance(src, (str, Path)):
        text = Path(src).read_text(encoding="utf-8")
    elif isinstance(src, io.IOBase) or hasattr(src, "read"):
        text = src.read()
    else:
        raise TypeError(f"cannot read a register log from {type(src).__name__}")
    writes = []
    for num, line in enumerate(text.splitlines(), start=1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) != 3:
            raise ValueError(f"bad register log line {num}: {line!r}")
        try:
            writes.append(RegWrite(*(int(field) for field in fields)))
        except ValueError as exc:
            raise ValueError(f"bad register log line {num}: {line!r}") from exc
    return writes
