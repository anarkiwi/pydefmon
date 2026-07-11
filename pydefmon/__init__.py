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
* :class:`DefmonPlayer` — per-frame SID register player that runs
  the tune's own relocatable replay on a py65 6502 (byte-exact
  against the sidtrace oracle over real HVSC tunes).

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

from pysidtracker import (
    RegWrite,
    read_reglog,
    register_writes_from_player,
    render_player_samples,
    render_player_wav,
    write_reglog,
)

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
from pydefmon.defmon_player import DefmonPlayer

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
    "__version__",
    "read_reglog",
    "register_writes_from_player",
    "render_player_samples",
    "render_player_wav",
    "write_reglog",
]

__version__ = "0.8.0"
