"""py65 register oracle for defMON ``.sid`` replays (test-only).

Thin wrapper over :func:`pysidtracker.oracle.register_grid`: runs a PSID/RSID
replay's init then its play routine at PAL frame rate on a clean-room py65
6502, sampling the 25 SID registers ``$D400..$D418`` at the end of each play
call to produce the ground-truth per-frame register grid the Goto80 decoder is
checked against. defMON's replay executes NMOS illegal opcodes
(SBX/SAX/ANC/SBC/LAX + ALR/ARR immediates and the multi-byte NOP illegals), so
``illegal_opcodes=True`` is required; ``register_grid`` implements them from
their documented behaviour. init runs with ``A = start_song - 1``.

Requires the ``emu`` extra (``pip install pydefmon[emu]``); importing raises
``ImportError`` when py65 is absent so callers can ``skipTest`` cleanly.
"""

from typing import List

from pysidtracker import SidImage
from pysidtracker.oracle import register_grid

# Import-time availability gate: register_grid needs py65 (the emu extra).
# Importing MPU eagerly here surfaces the missing dependency as ImportError so
# callers can ``skipTest``.
from py65.devices.mpu6502 import MPU  # noqa: F401


class Oracle:
    """Per-frame SID register grid for a PSID/RSID defMON replay."""

    def __init__(self, raw: bytes) -> None:
        self.image = SidImage.from_bytes(raw)
        self.subtune = (self.image.header.start_song or 1) - 1

    def grid(self, frames: int) -> List[List[int]]:
        """Return ``frames`` rows of the 25 SID registers ($D400..$D418)."""
        return register_grid(
            self.image, frames, subtune=self.subtune, illegal_opcodes=True
        )
