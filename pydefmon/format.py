"""defMON registration for the generic ``pysidtracker`` command-line tool.

pydefmon ships no CLI of its own: it registers a
:class:`~pysidtracker.SidFormat` on the ``pysidtracker.formats`` entry-point
group (see ``pyproject.toml``), so the shared ``pysidtracker info / reglog /
wav`` tool recognises a defMON replay by content and renders it through
:class:`~pydefmon.DefmonPlayer`; ``pysidtracker snapshot`` digests the decoded
RAM image and the canonical song grammar (:class:`~pydefmon.Tune`) as probes.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from pysidtracker import SidFormat, SidImage

from pydefmon import _sid_format
from pydefmon.defmon import DefmonSidParser, DefmonSong
from pydefmon._variant import detect_variant
from pydefmon.defmon_player import DefmonPlayer
from pydefmon.tune import Tune


def _metadata(song: DefmonSong) -> Sequence:
    """The shared ``(name, author, released, load, init, play)`` info block.

    defMON's runtime model carries no header metadata, so it is read from the
    original container: the PSID/RSID header when present, the player located
    by signature for a pack-save PRG, else just the load address.

    Returns:
        ``(name, author, released, load, init, play)``; empty name/author/
        released for bare PRG containers.
    """
    raw = song.image_bytes
    if raw and raw[:4] in (b"PSID", b"RSID"):
        header = SidImage.from_bytes(raw).header
        if header is not None:
            init = header.init_address or header.real_load_address
            return (
                header.name,
                header.author,
                header.released,
                header.real_load_address,
                init,
                header.play_address or init,
            )
    if raw:
        replay = _sid_format.prg_replay(raw)
        if replay is not None:
            load, base = replay
            return ("", "", "", load, base, base + 3)
    load = song.load_address
    return ("", "", "", load, load, load)


def _facet_sites(song: DefmonSong) -> Mapping[int, str]:
    """``{addr: facet}``: the code address the player signature was detected at."""
    return {} if song.signature is None else {song.signature: "signature"}


def defmon_format() -> SidFormat:
    """The :class:`~pysidtracker.SidFormat` describing defMON for the generic CLI."""
    return SidFormat(
        name="defmon",
        parser=DefmonSidParser(),
        player=lambda song: DefmonPlayer(song.image_bytes, subtune=song.subtune),
        metadata=_metadata,
        variant=detect_variant,
        facet_sites=_facet_sites,
        describe=lambda song: [f"snapshot: {len(song.snapshot)} bytes"],
        probes={
            "snapshot": lambda song: bytes(song.snapshot),
            "tune": lambda song: str(Tune.from_song(song).canonical()).encode(),
        },
    )
