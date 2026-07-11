"""Frame-accurate defMON player: run the tune's own relocatable replay.

A defMON tune ships (in HVSC) as a PSID/RSID *replay* -- a relocatable player
plus compacted tune data. :class:`DefmonPlayer` mounts that image, runs its
``init`` once, then runs its ``play`` routine once per frame on a py65 6502,
sampling the 25 SID registers ``$D400..$D418`` after each call. This runs the
replay's *own* machine code, so every defMON driver variation renders byte-for-
byte -- no per-subsystem Python transcription to keep faithful.

It is a thin :class:`pysidtracker.EmuPlayer` -- the shared "run the tune's own
driver on py65" player -- which owns the CPU build (with the raster / SID-read
observers and the NMOS illegal opcodes defMON executes), the post-init snapshot,
the per-frame diffing ``play_frame``, and the ``render_grid`` driver. Only the
defMON specifics live here: resolving a source to a :class:`SidImage`, wiring the
header's init/play addresses, and the tune's play-routine cadence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

from pysidtracker import EmuPlayer, SidImage
from pysidtracker.cadence import playroutine_cadence

from pydefmon.defmon import DefmonError

PlayerSource = Union[bytes, bytearray, str, Path, SidImage]


def _as_image(source: PlayerSource) -> SidImage:
    """Resolve a player source (``SidImage`` / bytes / path) to a ``SidImage``."""
    if isinstance(source, SidImage):
        return source
    if isinstance(source, (bytes, bytearray)):
        return SidImage.from_bytes(bytes(source))
    return SidImage.from_bytes(Path(source).read_bytes())


class DefmonPlayer(EmuPlayer):
    """Per-frame SID register player for a defMON PSID/RSID replay.

    Construct from replay bytes, a path, or a :class:`~pysidtracker.SidImage`;
    the header's ``startSong`` selects the subtune unless ``subtune`` overrides
    it. Drive it with the inherited :meth:`~pysidtracker.MemPlayer.play_frame`
    (per-frame ``(reg, value)`` writes, ``reg`` a ``0..24`` SID register offset)
    or :meth:`~pysidtracker.MemPlayer.render_grid` (per-frame 25-register grid);
    both come from :class:`~pysidtracker.EmuPlayer`, byte-exact against the
    sidtrace oracle over real HVSC tunes.

    Raises :class:`~pydefmon.DefmonError` for a source with no init/play
    routine (e.g. a bare ``.prg`` editor workfile, which carries no player --
    read those with :class:`~pydefmon.DefmonSong`).
    """

    def __init__(self, source: PlayerSource, *, subtune: "int | None" = None) -> None:
        image = _as_image(source)
        header = image.header
        if header is None or not (header.init_address or header.real_load_address):
            raise DefmonError(
                "DefmonPlayer needs a PSID/RSID replay with an init routine; "
                "a bare .prg workfile carries no player -- use DefmonSong to read it"
            )
        self._image = image
        init = header.init_address or header.real_load_address
        play = header.play_address or init
        if subtune is None:
            subtune = (header.start_song or 1) - 1
        super().__init__(
            image.image, image.load, init, play, subtune, illegal_opcodes=True
        )

    @property
    def cycles_per_frame(self) -> int:
        """CPU cycles between consecutive play calls (the play-routine cadence).

        From :func:`pysidtracker.cadence.playroutine_cadence`: the tune's CIA
        Timer-A latch (``latch + 1``) if init programs one, else the PAL/NTSC
        video frame. Multi-speed tunes report their per-NMI period here.
        """
        return playroutine_cadence(self._image).cycles_per_call
