"""Python reader / writer / player for defMON C64 tracker tunes.

Top-level API:

* :class:`DefmonSong` — read / edit / write defMON ``.prg`` files and
  ``.sid`` replays via :meth:`DefmonSong.from_file` / :meth:`DefmonSong.to_file`.
  A ``.prg`` and a ``.sid`` of the same tune are the same music in two
  content forms (:attr:`DefmonSong.form`); compare them with
  :meth:`DefmonSong.diff`, not a raw ``snapshot`` ``==``.
* :class:`PatternEvent` — one 4-byte step in a pattern body
  (flag + slot_a + slot_b + note).
* :class:`SidtabRow` — one 15-byte sidTAB row (per-frame envelope
  / timbre / filter program; bitmap-encoded columns).
* Stereo (2SID) editing: ``DefmonSong.arranger_v4`` / ``v5`` / ``v6``
  (the SID#2 voices' per-step pattern arrays), ``stereo_enable`` /
  ``set_stereo``, ``sid2_base`` / ``set_sid2_base``, plus the per-tune
  tempo fields ``cia2_timer`` / ``sub_frame_count`` on the
  runtime-state page.
* :class:`SidcallFrame` — one row of a sidTAB cascade walk
  starting from a given row index.
* :class:`DefmonPlayer` — per-frame SID register player that runs
  the tune's own relocatable replay on a jennings 6502 (byte-exact
  against the sidtrace oracle over real HVSC tunes).

Constants:

* :data:`LOAD_ADDRESS` — ``$1800``, where defMON tunes load.
* :data:`PREHISTORIC_LOAD_ADDRESS` — ``$1700``, the raw worktune save base
  of the prehistoric 2008 builds (detected and rejected with a specific
  :class:`DefmonParseError`).
* :data:`STANDARD_SNAPSHOT_END` / :data:`STANDARD_SNAPSHOT_SIZE`
  — the runtime RAM image end (``$7167``) and length (22887).
* :data:`NOTE_PITCH_LO` / :data:`NOTE_PITCH_HI` — 256-byte
  note-to-SID-freq LUTs the player walks (full 8-bit index range).

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
    DIFF_REGIONS,
    DefmonError,
    DefmonParseError,
    DefmonSidParser,
    DefmonSong,
    LOAD_ADDRESS,
    NOTE_PITCH_HI,
    NOTE_PITCH_LO,
    PREHISTORIC_LOAD_ADDRESS,
    PREHISTORIC_WORKTUNE_ERROR,
    PatternEvent,
    RegionDiff,
    SidcallFrame,
    SidtabRow,
    STANDARD_SNAPSHOT_END,
    STANDARD_SNAPSHOT_SIZE,
)
from pydefmon.defmon_player import DefmonPlayer

__all__ = [
    "DIFF_REGIONS",
    "DefmonError",
    "DefmonParseError",
    "DefmonPlayer",
    "DefmonSidParser",
    "DefmonSong",
    "LOAD_ADDRESS",
    "NOTE_PITCH_HI",
    "NOTE_PITCH_LO",
    "PREHISTORIC_LOAD_ADDRESS",
    "PREHISTORIC_WORKTUNE_ERROR",
    "PatternEvent",
    "RegionDiff",
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

__version__ = "0.10.3"
