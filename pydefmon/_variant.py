"""defMON variant facets (private).

Each :class:`~pysidtracker.facets.Facet` is one axis of how a
:class:`~pydefmon.defmon.DefmonSong` was read -- the container it came from,
the replay layout strategy that decoded it, where the player signature sits and
the data base derived from it, and the post-LOAD pass gates. :func:`detect_variant`
yields the song's :class:`~pysidtracker.facets.Variant`: a value and a source
(``detected@$addr`` / ``inferred(...)`` / ``default``) per facet, which the survey
``info`` reports as ``<facet>`` and ``<facet>.src``.
"""

from __future__ import annotations

from typing import Any, Optional

from pysidtracker.facets import (
    Facet,
    FacetRegistry,
    Finding,
    Variant,
    detected,
    inferred,
)
from pysidtracker.locate import VARIANT_FACET

from pydefmon._sid_format import _DATA_BASE_FROM_SIGNATURE
from pydefmon._layout import (
    GATE_PAT_BASE_ADDR,
    GATE_PATTERN_MARKER_ADDR,
    GATE_SONG_POSITION_ADDR,
    LOAD_ADDRESS,
)

#: Container name -> its load address, for the PRG-wrapped containers.
_PRG_CONTAINERS = {
    0x1000: "prg-raw",
    0x0801: "prg-executable",
    LOAD_ADDRESS: "prg-workfile",
}
CONTAINERS = ("psid", "rsid", "prg", *_PRG_CONTAINERS.values())


def _container(song: Any) -> Optional[Finding]:
    raw = song.image_bytes
    if raw[:4] in (b"PSID", b"RSID"):
        return Finding(raw[:4].decode().lower(), inferred("container magic"))
    if len(raw) < 2:
        return None
    load = int.from_bytes(raw[:2], "little")
    name = _PRG_CONTAINERS.get(load, "prg")
    return Finding(name, inferred(f"prg load address ${load:04X}"))


def _layout(song: Any) -> Optional[Finding]:
    entry = song.layout_entry
    return None if entry is None else Finding(entry.value, entry.source)


def _signature(song: Any) -> Optional[Finding]:
    sig = song.signature
    return None if sig is None else Finding(sig, detected(sig))


def _data_base(song: Any) -> Optional[Finding]:
    sig = song.signature
    if sig is None:
        return None
    return Finding(sig + _DATA_BASE_FROM_SIGNATURE, detected(sig))


def _gate(addr: int):
    def detect(song: Any) -> Optional[Finding]:
        return Finding(song.snapshot[addr - LOAD_ADDRESS], detected(addr))

    return detect


FACETS = FacetRegistry(
    [
        Facet("container", "layout", domain=CONTAINERS, detector=_container),
        Facet(
            VARIANT_FACET,
            "layout",
            domain=("editor", "goto80"),
            detector=_layout,
        ),
        Facet("signature", "layout", detector=_signature),
        Facet("data_base", "layout", detector=_data_base),
        Facet(
            "gate_song_position",
            "init",
            detector=_gate(GATE_SONG_POSITION_ADDR),
        ),
        Facet("gate_pat_base", "init", detector=_gate(GATE_PAT_BASE_ADDR)),
        Facet(
            "gate_pattern_marker",
            "init",
            detector=_gate(GATE_PATTERN_MARKER_ADDR),
        ),
    ]
)


def detect_variant(song: Any) -> Variant:
    """The :class:`~pysidtracker.facets.Variant` of ``song``."""
    return FACETS.detect(song, family="defmon")
