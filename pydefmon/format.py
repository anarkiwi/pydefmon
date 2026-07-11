"""defMON registration for the generic ``pysidtracker`` command-line tool.

pydefmon ships no CLI of its own: it registers a
:class:`~pysidtracker.SidFormat` on the ``pysidtracker.formats`` entry-point
group (see ``pyproject.toml``), so the shared ``pysidtracker info / reglog /
wav`` tool recognises a defMON replay by content and renders it through
:class:`~pydefmon.DefmonPlayer`.
"""

from __future__ import annotations

from typing import Sequence

from pysidtracker import SidFormat, SidImage

from pydefmon.defmon import DefmonSidParser, DefmonSong
from pydefmon.defmon_player import DefmonPlayer


def _metadata(song: DefmonSong) -> Sequence:
    """The shared ``(name, author, released, load, init, play)`` info block.

    defMON's runtime model carries no header metadata, so it is read from the
    original replay container (empty strings / the load address for a bare
    ``.prg`` workfile, which has no PSID/RSID header)."""
    header = SidImage.from_bytes(song.image_bytes).header if song.image_bytes else None
    if header is None:
        load = song.load_address
        return ("", "", "", load, load, load)
    init = header.init_address or header.real_load_address
    return (
        header.name,
        header.author,
        header.released,
        header.real_load_address,
        init,
        header.play_address or init,
    )


def defmon_format() -> SidFormat:
    """The :class:`~pysidtracker.SidFormat` describing defMON for the generic CLI."""
    return SidFormat(
        name="defmon",
        parser=DefmonSidParser(),
        player=lambda song: DefmonPlayer(song.image_bytes),
        metadata=_metadata,
        describe=lambda song: [f"snapshot: {len(song.snapshot)} bytes"],
    )
