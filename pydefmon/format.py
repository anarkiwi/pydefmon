"""defMON registration for the generic ``pysidtracker`` command-line tool.

pydefmon ships no CLI of its own: it registers a
:class:`~pysidtracker.SidFormat` on the ``pysidtracker.formats`` entry-point
group (see ``pyproject.toml``), so the shared ``pysidtracker info / reglog /
wav`` tool recognises a defMON replay by content and renders it through
:class:`~pydefmon.DefmonPlayer`; ``pysidtracker snapshot`` digests the decoded
RAM image and the canonical song grammar (:class:`~pydefmon.Tune`), which is
also what ``pysidtracker equiv`` compares; ``pysidtracker grammar`` lists the
arranger rows, pattern bodies and sidTAB rows the player reads through ``streams.lark``.
"""

from __future__ import annotations

from typing import Iterator, Mapping, Sequence

from pysidtracker import SidFormat, SidImage
from pysidtracker.formats import SidFormatError, SpecBinding

from pydefmon import _sid_format
from pydefmon._layout import (
    ArrangerRows,
    LOAD_ADDRESS,
    PATTERN_BANK,
    PATTERN_STRIDE,
    SIDTAB_BANK,
    SIDTAB_ROW_STRIDE,
    STREAMS,
)
from pydefmon.defmon import DefmonSidParser, DefmonSong
from pydefmon._variant import detect_variant
from pydefmon.defmon_player import DefmonPlayer, replay_image
from pydefmon.spec import player_bases, spec, spec_bind, spec_entries
from pydefmon.tune import ARRANGERS, Tune, _page


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


def _streams(song: DefmonSong) -> Iterator[tuple]:
    """The arranger rows, pattern bodies and sidTAB rows the player reads, as ``streams.lark`` models."""
    tune = Tune.from_song(song)
    snap = song.unpacked_snapshot()
    for chip, (rows, pages) in enumerate(zip(tune.chips, ARRANGERS), 1):
        columns = [_page(snap, page) for page in pages]
        body = ArrangerRows(tuple(col[y] for col in columns) for y in rows)
        yield f"arranger sid{chip}", body, "arranger"
    for p in tune.patterns:
        start = PATTERN_BANK + p * PATTERN_STRIDE - LOAD_ADDRESS
        steps = STREAMS.parse_prefix(snap[start : start + PATTERN_STRIDE], "editor")
        yield f"pattern ${p:02x}", steps.model, "editor"
    for y in tune.rows:
        start = SIDTAB_BANK + y * SIDTAB_ROW_STRIDE - LOAD_ADDRESS
        row = STREAMS.parse_prefix(snap[start : start + SIDTAB_ROW_STRIDE], "row")
        yield f"row ${y:02x}", row.model, "row"


def _spec(song: DefmonSong) -> SpecBinding:
    """The playspec of the song's own single-SID replay.

    Raises:
        SidFormatError: the replay plays more than one copy of the player (2SID).
    """
    image = replay_image(SidImage.from_bytes(song.image_bytes))
    if len(player_bases(image.image)) > 1:
        raise SidFormatError("a stereo (2SID) replay has no single-SID playspec")
    subtune = song.subtune
    if subtune is None:
        subtune = (image.header.start_song or 1) - 1
    raw = song.image_bytes
    container = raw if raw[:4] in (b"PSID", b"RSID") else None
    return SpecBinding(
        spec(),
        spec_bind,
        image.image,
        spec_entries,
        subtune=subtune,
        sid=container,
    )


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
        probes={"snapshot": lambda song: bytes(song.snapshot)},
        grammar=STREAMS,
        streams=_streams,
        spec=_spec,
        canonical=lambda song: str(Tune.from_song(song).canonical()),
    )
