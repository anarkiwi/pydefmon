"""Frame-accurate defMON player: run the tune's own relocatable replay.

A defMON tune ships (in HVSC) as a PSID/RSID *replay* -- a relocatable player
plus compacted tune data. :class:`DefmonPlayer` mounts that image, runs its
``init`` once, then runs its ``play`` routine once per frame on a jennings 6502,
sampling the 25 SID registers ``$D400..$D418`` after each call. This runs the
replay's *own* machine code, so every defMON driver variation renders byte-for-
byte -- no per-subsystem Python transcription to keep faithful.

It is a thin :class:`pysidtracker.EmuPlayer` -- the shared "run the tune's own
driver on jennings" player -- which owns the CPU build (with the raster / SID-read
observers and the NMOS illegal opcodes defMON executes), the post-init snapshot,
the per-frame diffing ``play_frame``, and the ``render_grid`` driver. Only the
defMON specifics live here: resolving a source to a :class:`SidImage`, wiring the
header's init/play addresses, and the tune's play-routine cadence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

from pysidtracker import EmuPlayer, SidImage
from pysidtracker import registers as reg
from pysidtracker.cadence import playroutine_cadence
from pysidtracker.header import write_psid

from pydefmon import _sid_format
from pydefmon.defmon import DefmonError, DefmonSong

PlayerSource = Union[bytes, bytearray, str, Path, SidImage, DefmonSong]


def _as_image(source: PlayerSource) -> SidImage:
    """Resolve a source (path/bytes/SidImage/DefmonSong) to a ``SidImage``.

    A packed-form :class:`DefmonSong` resolves to the replay it parsed
    from; an editor-form one (``.prg`` worktune) carries no player and
    must be packed first via :meth:`DefmonSong.to_replay`.
    """
    if isinstance(source, DefmonSong):
        if source.form == "packed" and source.image_bytes:
            return SidImage.from_bytes(source.image_bytes)
        raise DefmonError(
            "DefmonPlayer needs a PSID/RSID replay with an init routine; "
            "a bare .prg workfile carries no player -- pack it with "
            "DefmonSong.to_replay(editor_image) or read its model with "
            "DefmonSong"
        )
    if isinstance(source, SidImage):
        return source
    if isinstance(source, (bytes, bytearray)):
        return SidImage.from_bytes(bytes(source))
    return SidImage.from_bytes(Path(source).read_bytes())


class DefmonPlayer(EmuPlayer):
    """Per-frame SID register player for a defMON PSID/RSID replay.

    Construct from replay bytes, a path, a :class:`~pysidtracker.SidImage`,
    or a packed-form :class:`~pydefmon.DefmonSong`;
    the header's ``startSong`` selects the subtune unless ``subtune`` overrides
    it. Play calls run at the tune's play-routine cadence and osc3/env3 reads
    come from the SID model the header names. Memory starts as sidplayfp's
    power-on RAM with the BASIC loader's zero-page pointers to the tune
    (:func:`pysidtracker.poweron.place_tune`).
    Drive it with the inherited :meth:`~pysidtracker.MemPlayer.play_frame`
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
            # Pack-save export PRG (raw $1000 / executable $0801): player located by signature.
            replay = _sid_format.prg_replay(bytes(image.container) + bytes(image.image))
            if replay is None:
                raise DefmonError(
                    "DefmonPlayer needs a PSID/RSID replay with an init routine; "
                    "a bare .prg workfile carries no player -- use DefmonSong to read it"
                )
            load, base = replay  # init = player base, play = base + 3, PAL + 8580
            psid = write_psid(
                load=load,
                init=base,
                play=base + 3,
                image=bytes(image.image),
                flags=0x20,
            )
            image = SidImage.from_bytes(psid)
            header = image.header
        init = header.init_address or header.real_load_address
        play = header.play_address or init
        if subtune is None:
            subtune = (header.start_song or 1) - 1
        super().__init__(
            image.image,
            image.load,
            init,
            play,
            subtune,
            illegal_opcodes=True,
            sid_model=reg.sid_model_for_flags(header.flags),
            cycles_per_frame=playroutine_cadence(
                image, subtune=subtune
            ).cycles_per_call,
        )

    @property
    def cycles_per_frame(self) -> int:
        """CPU cycles between consecutive play calls (the play-routine cadence).

        ``cycles_per_call`` of :func:`pysidtracker.cadence.playroutine_cadence`,
        derived from how the tune programs its interrupts.
        """
        return self._period
