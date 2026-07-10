"""Python reader / writer / player for defMON C64 tracker tunes.

Top-level API:

* :class:`DefmonSong` — read / edit / write defMON ``.prg`` files
  via :meth:`DefmonSong.from_file` and :meth:`DefmonSong.to_file`.
* :class:`PatternEvent` — one 4-byte step in a pattern body
  (flag + slot_a + slot_b + note).
* :class:`SidtabRow` — one 15-byte sidTAB row (per-frame envelope
  / timbre / filter program; bitmap-encoded columns).
* :class:`SidcallFrame` — one row of a sidTAB cascade walk
  starting from a given row index.
* :class:`DefmonPlayer` — frame-accurate per-NMI player IRQ model
  (byte-faithful against the real defMON binary running in
  asid-vice, verified by the integration test suite).
* :class:`Voice` — per-voice runtime record exposed for
  introspection / programmatic poking.

Constants:

* :data:`LOAD_ADDRESS` — ``$1800``, where defMON tunes load.
* :data:`STANDARD_SNAPSHOT_END` / :data:`STANDARD_SNAPSHOT_SIZE`
  — the runtime RAM image end (``$7167``) and length (22887).
* :data:`NOTE_PITCH_LO` / :data:`NOTE_PITCH_HI` — 128-byte
  note-to-SID-freq LUTs the player walks.

For raw byte-level work on defMON's ``$D6C9`` LOAD codec, see
:mod:`pydefmon._load_format` (private). Most users only need
:class:`DefmonSong` and :class:`DefmonPlayer`.
"""

from pydefmon.defmon import (
    DefmonError,
    DefmonSidParser,
    DefmonSong,
    LOAD_ADDRESS,
    NOTE_PITCH_HI,
    NOTE_PITCH_LO,
    PatternEvent,
    SidcallFrame,
    SidtabRow,
    STANDARD_SNAPSHOT_END,
    STANDARD_SNAPSHOT_SIZE,
)
from pydefmon.defmon_player import DefmonPlayer, Voice
from pydefmon.reglog import (
    RegWrite,
    iter_register_writes,
    read_reglog,
    write_reglog,
)

__all__ = [
    "DefmonError",
    "DefmonPlayer",
    "DefmonSidParser",
    "DefmonSong",
    "LOAD_ADDRESS",
    "NOTE_PITCH_HI",
    "NOTE_PITCH_LO",
    "PatternEvent",
    "RegWrite",
    "SidcallFrame",
    "SidtabRow",
    "STANDARD_SNAPSHOT_END",
    "STANDARD_SNAPSHOT_SIZE",
    "Voice",
    "__version__",
    "iter_register_writes",
    "read_reglog",
    "write_reglog",
]

__version__ = "0.5.0"
