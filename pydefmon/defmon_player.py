"""Frame-accurate defMON player: run the tune's own relocatable replay.

A defMON tune ships (in HVSC) as a PSID/RSID *replay* -- a relocatable player
plus compacted tune data. :class:`DefmonPlayer` mounts that image, runs its
``init`` once, then runs its ``play`` routine once per frame on a py65 6502,
sampling the 25 SID registers ``$D400..$D418`` after each call. This runs the
replay's *own* machine code, so every defMON driver variation renders byte-for-
byte -- no per-subsystem Python transcription to keep faithful.

defMON's replay executes NMOS illegal opcodes (SBX/SAX/ANC/ALR/ARR/LAX/SBC plus
the multi-byte NOP illegals), so those are installed on the CPU via the shared
:mod:`pysidtracker.oracle` opcode set rather than duplicated here.

The class derives from :class:`pysidtracker.MemPlayer`: it inherits the flat
64 KiB image, the post-init snapshot, the per-frame diffing ``play_frame``, and
the ``iter_frames`` / ``render_grid`` drivers. Only ``_init`` and ``_frame`` --
the two defMON-specific steps -- are implemented here.

CLI::

    pydefmon-player INPUT.sid OUTPUT.wav [--seconds 60] [--model 6581|8580]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Union

from pysidtracker import MemPlayer, SidImage
from pysidtracker import registers as reg
from pysidtracker.audio import render_wav as _render_wav, resolve_device
from pysidtracker.cadence import playroutine_cadence
from pysidtracker.oracle import _patch_illegals
from pysidtracker.trace import _run_to_rts

from pydefmon.defmon import DefmonError

SID_REG_BASE = reg.SID_BASE

# libsidplayfp / sidtrace run the emulated 6502 with a generous ceiling; a
# single init or play never approaches it, but it guards against a runaway.
_MAX_CYCLES = 8_000_000

# V3 oscillator / envelope readback ($D41B/$D41C): the replay reads these for
# entropy; mirror the sidtrace oracle's cycle-derived stand-in.
_SID_READ_REGS = (0xD41B, 0xD41C)

PlayerSource = Union[bytes, bytearray, str, Path, SidImage]


def _as_image(source: PlayerSource) -> SidImage:
    """Resolve a player source (``SidImage`` / bytes / path) to a ``SidImage``."""
    if isinstance(source, SidImage):
        return source
    if isinstance(source, (bytes, bytearray)):
        return SidImage.from_bytes(bytes(source))
    return SidImage.from_bytes(Path(source).read_bytes())


class DefmonPlayer(MemPlayer):
    """Per-frame SID register player for a defMON PSID/RSID replay.

    Construct from replay bytes, a path, or a :class:`~pysidtracker.SidImage`;
    the header's ``startSong`` selects the subtune unless ``subtune`` overrides
    it. Drive it with the inherited :meth:`~pysidtracker.MemPlayer.play_frame`
    (per-frame ``(reg, value)`` writes, ``reg`` a ``0..24`` SID register offset)
    or :meth:`~pysidtracker.MemPlayer.render_grid` (per-frame 25-register grid).

    Raises :class:`~pydefmon.DefmonError` for a source with no init/play
    routine (e.g. a bare ``.prg`` editor workfile, which carries no player --
    read those with :class:`~pydefmon.DefmonSong`).
    """

    def __init__(self, source: PlayerSource, *, subtune: "int | None" = None) -> None:
        image = _as_image(source)
        if image.header is None or not (
            image.header.init_address or image.header.real_load_address
        ):
            raise DefmonError(
                "DefmonPlayer needs a PSID/RSID replay with an init routine; "
                "a bare .prg workfile carries no player -- use DefmonSong to read it"
            )
        self._image = image
        self._mpu = None
        self._cpu_mem = None
        self._play_address = 0
        if subtune is None:
            subtune = (image.header.start_song or 1) - 1
        # MemPlayer mounts image.image at image.load into a fresh 64 KiB buffer
        # and seeds $D418 to the PSID driver's cold-start volume, then calls
        # ``_init`` (which builds the CPU over that buffer) and snapshots.
        super().__init__(image.image, image.load, subtune)

    def _build_cpu(self) -> None:
        """Build the py65 CPU over the player's memory (illegal opcodes on)."""
        from py65.devices.mpu6502 import MPU
        from py65.memory import ObservableMemory

        subject = self._mem
        mem = ObservableMemory(subject=subject)
        mpu = MPU(memory=mem)
        _patch_illegals(mpu)

        def _on_raster(addr):
            line = (mpu.processorCycles // 63) % 312
            if addr == reg.VIC_RASTER:
                return line & 0xFF
            return (subject[reg.VIC_CONTROL_1] & 0x7F) | (((line >> 8) & 1) << 7)

        def _on_sid_read(addr):  # pylint: disable=unused-argument
            return (mpu.processorCycles >> 3) & 0xFF

        mem.subscribe_to_read([reg.VIC_CONTROL_1, reg.VIC_RASTER], _on_raster)
        mem.subscribe_to_read(list(_SID_READ_REGS), _on_sid_read)
        self._mpu = mpu
        self._cpu_mem = mem

    def _init(self, subtune: int) -> None:
        self._build_cpu()
        header = self._image.header
        init_address = header.init_address or header.real_load_address
        self._play_address = header.play_address or init_address
        _run_to_rts(self._mpu, self._cpu_mem, init_address, subtune, _MAX_CYCLES)

    def _frame(self) -> None:
        _run_to_rts(self._mpu, self._cpu_mem, self._play_address, 0, _MAX_CYCLES)

    def render_grid(self, nframes: int) -> List[List[int]]:
        """Per-frame 25-register grid, pulse-width-high nibble-masked.

        Overrides :meth:`~pysidtracker.MemPlayer.render_grid` to mask the
        pulse-width-high registers (``$D403/$D40A/$D411``) to their low nibble
        -- the SID ignores the upper bits and the sidtrace oracle records them
        masked, so this keeps the grid directly comparable to the oracle.
        """
        rows = super().render_grid(nframes)
        for row in rows:
            for i in reg.PW_HI_REGS:
                row[i] &= 0x0F
        return rows

    @property
    def cycles_per_frame(self) -> int:
        """CPU cycles between consecutive play calls (the play-routine cadence).

        From :func:`pysidtracker.cadence.playroutine_cadence`: the tune's CIA
        Timer-A latch (``latch + 1``) if init programs one, else the PAL/NTSC
        video frame. Multi-speed tunes report their per-NMI period here.
        """
        return playroutine_cadence(self._image).cycles_per_call


def render_wav(
    source: PlayerSource,
    dst,
    *,
    seconds: float = 60.0,
    model: str = "8580",
    device=None,
) -> Path:
    """Render a defMON replay to a WAV file via :mod:`pysidtracker.audio`.

    The player's per-frame ``(reg, value)`` writes are emulated by the shared
    :func:`pysidtracker.audio.render_wav` (pyresidfp -- the ``[wav]`` extra --
    unless a ``device`` is supplied). Returns the written path.
    """
    player = DefmonPlayer(source)
    cpf = player.cycles_per_frame
    dev = resolve_device(device=device, model=model)
    clock_hz = float(getattr(dev, "clock_frequency", reg.PAL_CLOCK_HZ))
    nframes = max(1, int(seconds * clock_hz / cpf))
    frames = (player.play_frame() for _ in range(nframes))
    return _render_wav(
        frames,
        dst,
        model=model,
        cycles_per_frame=cpf,
        clock_frequency=clock_hz,
        device=dev,
    )


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="Render a defMON .sid replay to WAV.")
    ap.add_argument("sid", type=Path, help="defMON PSID/RSID replay")
    ap.add_argument("wav", type=Path, help="output WAV")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--model", default="8580", choices=("6581", "8580"))
    args = ap.parse_args(argv)
    path = render_wav(args.sid, args.wav, seconds=args.seconds, model=args.model)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
