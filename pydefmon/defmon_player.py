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

from functools import lru_cache

from pysidtracker import EmuPlayer, SidImage, SidParseError
from pysidtracker import registers as reg
from pysidtracker.cadence import playroutine_cadence
from pysidtracker.header import write_psid
from pysidtracker.playspec import SpecError, SpecPlayer, parse

from pydefmon import _sid_format
from pydefmon._spec_layout import defmon_binding, play_entries, player_base, spec_text
from pydefmon.defmon import DefmonError, DefmonSong

#: Play calls closer together than this can overrun their period; the spec costs no
#: cycles, so it cannot tell when the replay's own code is late and drops a call.
MIN_SPEC_PERIOD = 4000

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


def replay_image(source: "SidImage | bytes") -> SidImage:
    """``source`` as a PSID image with init/play (export PRGs get a synthesized header).

    Raises :class:`~pydefmon.DefmonError` for a bare ``.prg`` workfile.
    """
    image = source if isinstance(source, SidImage) else SidImage.from_bytes(source)
    header = image.header
    if header is not None and (header.init_address or header.real_load_address):
        return image
    # Pack-save export PRG (raw $1000 / executable $0801): player located by signature.
    replay = _sid_format.prg_replay(bytes(image.container) + bytes(image.image))
    if replay is None:
        raise DefmonError(
            "DefmonPlayer needs a PSID/RSID replay with an init routine; "
            "a bare .prg workfile carries no player -- use DefmonSong to read it"
        )
    load, base = replay  # init = player base, play = base + 3, PAL + 8580
    psid = write_psid(
        load=load, init=base, play=base + 3, image=bytes(image.image), flags=0x20
    )
    return SidImage.from_bytes(psid)


@lru_cache(maxsize=None)
def _spec():
    return parse(spec_text("defmon"))


def _entries(view):
    base = player_base(view)
    try:
        return set() if base < 0 else play_entries(view, base)
    except ValueError:
        return set()


def _bind(view, entry):
    return defmon_binding(view, player_base(view), entry)


class DefmonPlayer(SpecPlayer):
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

    Each play call runs ``pydefmon/specs/defmon.play`` bound to the replay (see
    :attr:`spec_backed`); a replay the spec does not describe runs its own 6502
    code instead, as :class:`~pysidtracker.EmuPlayer` does.

    Raises :class:`~pydefmon.DefmonError` for a source with no init/play
    routine (e.g. a bare ``.prg`` editor workfile, which carries no player --
    read those with :class:`~pydefmon.DefmonSong`).
    """

    def __init__(self, source: PlayerSource, *, subtune: "int | None" = None) -> None:
        image = replay_image(_as_image(source))
        header = image.header
        init = header.init_address or header.real_load_address
        play = header.play_address or init
        if subtune is None:
            subtune = (header.start_song or 1) - 1
        super().__init__(
            _spec(),
            image.image,
            image.load,
            init,
            play,
            subtune,
            bind=_bind,
            entries=_entries,
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

    @property
    def spec_backed(self) -> bool:
        """Whether play calls run the playspec (else the replay's own 6502)."""
        return self.entry is not None

    def _init(self, subtune: int) -> None:
        memory = bytes(self._mem)
        try:
            try:
                super()._init(subtune)
            except SidParseError:
                # A wrapper that skips the player on its first call reaches no entry.
                entries = self._entries()
                if not entries:
                    raise
                self.entry = min(entries)
            if self._period and self._period < MIN_SPEC_PERIOD:
                raise SidParseError("play calls too close for the spec")
            self._instance(self.entry)
        except (SidParseError, SpecError, ValueError, LookupError):
            self._mem[:] = memory
            self.entry = None
            EmuPlayer._init(self, subtune)  # pylint: disable=protected-access

    def _frame(self) -> None:
        if self.entry is None:
            EmuPlayer._frame(self)  # pylint: disable=protected-access
        else:
            super()._frame()
