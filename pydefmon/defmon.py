"""defMON tune reader / writer.

A defMON tune is a C64 PRG file that loads at ``$1800`` and contains
an ``$D6C9``-encoded payload (see :mod:`pydefmon._load_format` —
byte-faithful round-trip against the real defMON loader). Once
decoded, the tune is a 22887-byte RAM image at ``$1800..$7166`` —
the same image defMON's editor sees after LOAD + its post-LOAD
fixup passes (``$CF42`` arranger-pointer rewrite, ``$D004``
pattern-pointer fill).

Quick start::

    from pydefmon import DefmonSong, PatternEvent

    # Read
    song = DefmonSong.from_file("tune.prg")
    for ev in song.pattern_events(0):
        print(ev)
    print(song.sidtab_row(0))

    # Edit
    song.set_step(0, v1=1, v2=2, v3=3)        # arranger row 0 plays patterns 1/2/3
    song.set_pattern_events(0, PatternEvent.silent_pattern())
    song.set_dl(0, 0x05)                      # sidTAB row 0 holds for 6 frames
    song.to_file("out.prg")

The runtime image is divided into well-defined regions; each has a
named accessor returning a ``memoryview`` slice you can read or
write through (writes mutate the underlying ``song.snapshot``):

============================  ====  ====================================================
Region                        Size  Purpose
============================  ====  ====================================================
``$1800..$18FF``               256  Per-row JP-target pointer lo (paired with $1900)
``$1900..$19FF``               256  Per-row JP marker ($11=active, $00=inactive/JP)
``$1A00..$1AFF``               256  Pattern pointer table (zero on disk, filled at runtime)
``$1B00..$1BFF``               256  V1 arranger (per-song-step pattern index)
``$1C00..$1CFF``               256  V2 arranger
``$1D00..$1DFF``               256  V3 arranger
 ``$1E00..$1EFF``               256  sidTAB DL byte (hold / STop per row)
 ``$1F00..$5EFF``             16384  128 pattern bodies (32 events × 4 bytes each)
 ``$5F00..$6DFF``              3840  256 sidTAB rows (15 bytes each)
 ``$6E00..$6EFF``               256  SID#2 voice-4 arranger (stereo)
 ``$6F00..$6FFF``               256  SID#2 voice-5 arranger (stereo)
 ``$7000..$70FF``               256  SID#2 voice-6 arranger (stereo)
 ``$7100..$7166``                103  runtime-state page (gates, tempo, sub-frame
                                    count, stereo flag, SID#2 base; editor state)
 ============================  ====  ====================================================

The arranger byte encoding: ``0`` means "silent voice on this song
step"; ``1..127`` are pattern numbers; ``$FF`` is the jump command
(``$1B00,Y=$FF`` with ``$1C00,Y=target_row`` and
``$1D00,Y=repeat_count``; ``count=0`` is unconditional infinite
loop).

Most user-facing edits go through the helper API (:meth:`set_step`,
:meth:`set_jump`, :meth:`set_dl`, :meth:`set_jp`,
:meth:`set_pattern_events`, :meth:`clear_arranger`,
:meth:`clear_dl`, :meth:`optimize`), but raw region access is
available for power users (:attr:`pattern_bodies`,
:attr:`sidtab_region`, :attr:`arranger_v1` .. ``v6`` (``v4``/``v5``/``v6``
are the SID#2 stereo voices), :attr:`sidtab_dl`, :attr:`sidtab_jp`,
and the runtime-state page fields :attr:`cia2_timer`,
:attr:`sub_frame_count`, :attr:`stereo_enable`, :attr:`sid2_base`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pysidtracker import BaseSidParser, SidError, SidImage, SidParseError, parse_prg
from pysidtracker.facets import Entry, Variant
from pysidtracker.header import write_psid
from pysidtracker.locate import STATIC, Strategy, locate
from pysidtracker.registers import PAL_CLOCK_HZ

from pydefmon import _packer
from pydefmon._load_format import (
    CodecError,
    decode_load_stream,
    encode_worktune,
)
from pydefmon import _sid_format
from pydefmon._goto80_format import decode_replay as decode_goto80_replay
from pydefmon._layout import (
    COLUMNS,
    GATE_PAT_BASE_ADDR,
    GATE_PATTERN_MARKER_ADDR,
    GATE_SONG_POSITION_ADDR,
    LOAD_ADDRESS,
    SNAPSHOT_END as STANDARD_SNAPSHOT_END,
    SNAPSHOT_SIZE as STANDARD_SNAPSHOT_SIZE,  # 22887
    STREAMS,
    RowColumns,
)
from pydefmon._variant import detect_variant

# Post-LOAD pass gates (select $CF42/$D004/$CFB8 when non-zero; see unpacked_snapshot)
GATE_SONG_POSITION = GATE_SONG_POSITION_ADDR - LOAD_ADDRESS
GATE_PAT_BASE = GATE_PAT_BASE_ADDR - LOAD_ADDRESS
GATE_PATTERN_MARKER = GATE_PATTERN_MARKER_ADDR - LOAD_ADDRESS

# Runtime-state page ($7100..$7166) per-tune playback fields, snapshot offsets.
CIA2_TIMER_LO = 0x715A - LOAD_ADDRESS  # CIA2 Timer-A reload, lo (per-NMI tempo)
CIA2_TIMER_HI = 0x715B - LOAD_ADDRESS  # CIA2 Timer-A reload, hi
SUB_FRAME_COUNT = 0x715C - LOAD_ADDRESS  # NMIs per main tick (1/2/4/8)
STEREO_ENABLE = 0x715D - LOAD_ADDRESS  # $00 mono / $01 stereo (2SID)
SID2_BASE_LO = 0x7164 - LOAD_ADDRESS  # SID#2 register base, lo
SID2_BASE_HI = 0x7165 - LOAD_ADDRESS  # SID#2 register base, hi

# Prehistoric 2008 builds (56/60 blocks): raw worktune saves at $1700, no $D6C9 codec.
PREHISTORIC_LOAD_ADDRESS = 0x1700
PREHISTORIC_WORKTUNE_ERROR = (
    "prehistoric 2008 defMON worktune (raw $1700 save from a 2008 build, "
    "no $D6C9 codec) is not supported; pydefmon only parses $1800 RLE "
    "worktunes and PSID/RSID replays"
)


@dataclass(frozen=True)
class RegionDiff:
    """A named RAM region's byte comparison between two songs (from ``diff``).

    ``name`` / ``base`` / ``size`` identify the region; ``differing`` counts
    unequal positions and ``identical`` is ``differing == 0``.
    """

    name: str
    base: int
    size: int
    differing: int

    @property
    def identical(self) -> bool:
        return self.differing == 0


#: Non-overlapping, address-ordered regions for ``diff``; sizes sum to STANDARD_SNAPSHOT_SIZE.
DIFF_REGIONS: tuple[tuple[str, int, int], ...] = (
    ("song_position", 0x1800, 0x200),
    ("pattern_ptrs", 0x1A00, 0x100),
    ("arranger_v1", 0x1B00, 0x100),
    ("arranger_v2", 0x1C00, 0x100),
    ("arranger_v3", 0x1D00, 0x100),
    ("sidtab_dl", 0x1E00, 0x100),
    ("patterns", 0x1F00, 0x4000),
    ("sidtab", 0x5F00, 0x6E00 - 0x5F00),
    ("arranger_v4", 0x6E00, 0x100),
    ("arranger_v5", 0x6F00, 0x100),
    ("arranger_v6", 0x7000, 0x100),
    ("runtime_state", 0x7100, STANDARD_SNAPSHOT_END - 0x7100),
)


# ----------------------------------------------------------------------
# Note → SID-frequency lookup tables (lo/hi byte each, 256 entries
# indexed by the raw 8-bit note value). The player's pitch oscillator
# reads these directly with unmasked 8-bit indexing, so the tables
# cover the full 0..255 range (first half is notes $01..$7F, second
# half is the continuation reachable via TR shifts).
# ``PatternEvent.sid_freq_word()`` computes from the same tables.
# ----------------------------------------------------------------------

_NOTE_PITCH_LO_HEX = (
    "464a4e53585d62686e757c838b939ca6afbac5d1ddeaf8071627394b5f748aa1"
    "bad4f00e2d4e7196bee7144274a9e01b5a9ce22d7bcf2785e851c137b438c459"
    "f79d4e0ad0a2816d677089b2ed3b9c13a04502dacee01164da763926408904b4"
    "9cc023c8b4eb724c801208683980459068d6e399002410ff0000000000000000"
    "464a4e53585d62686e757c838b939ca6afbac5d1ddeaf8071627394b5f748aa1"
    "bad4f00e2d4e7196bee7144274a9e01b5a9ce22d7bcf2785e851c137b438c459"
    "f79d4e0ad0a2816d677089b2ed3b9c13a04502dacee01164da763926408904b4"
    "9cc023c8b4eb724c801208683980459068d6e399002410ff0000000000000000"
)
_NOTE_PITCH_HI_HEX = (
    "0000000000000000000000000000000000000000000000010101010101010101"
    "010101020202020202020303030303040404040505050606060707080809090a"
    "0a0b0c0d0d0e0f10111213141517181a1b1d1f20222427292b2e3134373a3e41"
    "45494e52575c62686e757c838b939ca5afb9c4d0ddeaf8ff85fba000b1fbf05a"
    "0101010101010101010101010101010101010101010101020202020202020202"
    "02020203030303030303040404040405050505060606070707080809090a0a0b"
    "0b0c0d0e0e0f1011121314151618191b1c1e20212325282a2c2f3235383b3f42"
    "464a4f53585d63696f767d848c949da6b0bac5d1deebf9ff86fca101b2fcf15b"
)
NOTE_PITCH_LO = bytes.fromhex(_NOTE_PITCH_LO_HEX)
NOTE_PITCH_HI = bytes.fromhex(_NOTE_PITCH_HI_HEX)
assert len(NOTE_PITCH_LO) == 256
assert len(NOTE_PITCH_HI) == 256


class DefmonError(SidError, ValueError):
    """Base class for pydefmon errors."""


class DefmonParseError(DefmonError, SidParseError):
    """Raised when a file does not look like a defMON tune, or cannot be decoded."""


def _replay_strategies(sites: tuple) -> tuple:
    """Replay layouts in the order tried: the editor-layout map first, then the
    Goto80 compact runtime encoding the editor-layout map can't read. Both
    decode from the one signature ``sites`` already scanned for."""

    def at_sites(_image):
        return sites

    return (
        Strategy(
            "editor",
            STATIC,
            _sid_format.depack_replay,
            candidates=at_sites,
        ),
        Strategy(
            "goto80",
            STATIC,
            decode_goto80_replay,
            candidates=at_sites,
        ),
    )


class DefmonSong:
    """A defMON tune — the 22887-byte runtime RAM image at
    ``$1800..$7166``.

    Construct from a ``.prg`` file via :meth:`from_file` /
    :meth:`from_bytes`, or empty via ``DefmonSong()`` for
    from-scratch authoring. Save via :meth:`to_file` /
    :meth:`to_bytes` — round-trips through the ``$D6C9`` LOAD codec
    so the file is byte-loadable by the real defMON.

    Region accessors (:attr:`pattern_bodies`,
    :attr:`arranger_v1`, etc.) return ``memoryview`` slices of the
    underlying ``snapshot`` buffer, so writes through them mutate
    the song in place. The structured edit API
    (:meth:`set_step`, :meth:`set_pattern_events`, :meth:`set_dl`,
    :meth:`set_jp`, :meth:`set_jump`) validates inputs and uses the
    canonical byte encodings; reach for region accessors only when
    you need raw byte-level control.
    """

    LOAD_ADDRESS: int = LOAD_ADDRESS

    def __init__(self, snapshot: bytes = b"") -> None:
        if len(snapshot) > STANDARD_SNAPSHOT_SIZE:
            raise ValueError(
                f"snapshot must be <= {STANDARD_SNAPSHOT_SIZE} bytes, "
                f"got {len(snapshot)}"
            )
        buf = bytearray(STANDARD_SNAPSHOT_SIZE)
        buf[: len(snapshot)] = snapshot
        self.snapshot: bytearray = buf
        self.decode_top: int = (
            STANDARD_SNAPSHOT_END - 1
        )  # highest addr; from_bytes narrows this to the file's decoded top
        # Original container bytes this song was parsed from (a PSID/RSID
        # replay, or a ``.prg`` workfile). Empty for from-scratch authoring.
        # A replay carries the player, so its play-routine cadence can be
        # derived by tracing init; a workfile cannot.
        self.image_bytes: bytes = b""
        # Replay provenance: where the player signature sits and the layout
        # strategy that decoded it (``None`` for a workfile / from-scratch song).
        self.signature: "int | None" = None
        self.layout_entry: Entry | None = None
        # The 0-based subtune a replay of ``image_bytes`` plays (None: its start song).
        self.subtune: "int | None" = None

    # ---- construction ----

    @classmethod
    def from_bytes(cls, raw: bytes) -> "DefmonSong":
        """Parse a defMON tune from raw container bytes, dispatching on container.

        Accepts a ``$1800`` ``.prg`` editor workfile (``$D6C9`` RLE body), a
        PSID/RSID ``.sid`` replay, and a defMON pack-save export PRG (the
        ``$1000`` raw / ``$0801`` executable SHIFT+P saves), the last two via
        :meth:`_from_replay`.

        Raises:
            DefmonParseError: if ``raw`` matches none of the containers, a
            replay has no defMON player signature, a prehistoric 2008 raw
            ``$1700`` worktune is presented, or the replay's data layout
            cannot be mapped to the editor layout.
        """
        if raw[:4] in (b"PSID", b"RSID"):
            return cls._from_replay(SidImage.from_bytes(raw), bytes(raw))
        if len(raw) < 4:
            raise DefmonParseError("file too short to contain a load address + body")
        load_addr, _body = parse_prg(raw)
        if load_addr == LOAD_ADDRESS:
            return cls._from_worktune(raw)
        if _sid_format.prg_replay(raw) is not None:
            return cls._from_replay(SidImage.from_prg(raw), bytes(raw))
        if load_addr == PREHISTORIC_LOAD_ADDRESS:
            raise DefmonParseError(PREHISTORIC_WORKTUNE_ERROR)
        raise DefmonParseError(
            f"unexpected load address ${load_addr:04X}: not a ${LOAD_ADDRESS:04X} "
            f"editor workfile and no defMON replay player found"
        )

    @classmethod
    def from_file(cls, path: str) -> "DefmonSong":
        with open(path, "rb") as f:
            return cls.from_bytes(f.read())

    @classmethod
    def from_sid_bytes(cls, raw: bytes) -> "DefmonSong":
        """Parse a defMON tune from a PSID/RSID ``.sid`` replay file.

        HVSC ships defMON tunes as PSID/RSID replay images (a relocatable
        player plus the tune's compacted data), a different container from
        the ``.prg`` editor workfile :meth:`from_bytes` decodes.

        Raises:
            DefmonParseError: if ``raw`` is not a recognised defMON replay
            (no player signature) or uses a packer variant whose compact data
            layout cannot be mapped to the editor layout.
        """
        return cls._from_replay(SidImage.from_bytes(raw), bytes(raw))

    @classmethod
    def _from_worktune(cls, raw: bytes) -> "DefmonSong":
        """Decode a ``$1800`` editor workfile body with the ``$D6C9`` LOAD codec."""
        _load_addr, body = parse_prg(raw)
        # Trailer bytes encode dest_lo and dest_hi_offset.
        dest_lo = body[-2]
        dest_hi = body[-1] + 0x18
        dest_start = (dest_hi << 8) | dest_lo
        src_end = LOAD_ADDRESS + len(body) - 1 - 4
        try:
            writes, _ = decode_load_stream(
                body,
                src_end_addr=src_end,
                src_floor=LOAD_ADDRESS,
                dest_start=dest_start,
            )
        except CodecError as e:
            raise DefmonParseError(f"LOAD-decode failed: {e}") from e
        song = cls()
        for addr, val in writes.items():
            if LOAD_ADDRESS <= addr < LOAD_ADDRESS + STANDARD_SNAPSHOT_SIZE:
                song.snapshot[addr - LOAD_ADDRESS] = val
        song.decode_top = min(
            max(dest_start, LOAD_ADDRESS), LOAD_ADDRESS + STANDARD_SNAPSHOT_SIZE - 1
        )
        song.image_bytes = bytes(raw)
        return song

    @classmethod
    def _from_replay(cls, image, container: bytes) -> "DefmonSong":
        """Re-expand a defMON replay image into the editor-layout song model."""
        signature = _sid_format.find_signature(image.mem)
        if signature < 0:
            raise DefmonParseError("not a defMON replay (player signature not found)")
        found = locate(image, _replay_strategies((signature,)), init=False)
        if not found.ok:
            raise DefmonParseError(
                "unrecognised defMON replay data layout "
                "(compact/indirect packer variant not supported): "
                + found.describe().replace("\n", "; ")
            )
        song = cls(found.layout)
        song.image_bytes = container
        song.signature = signature
        song.layout_entry = found.variant
        return song

    # ---- serialization ----

    def to_bytes(self) -> bytes:
        """Serialize a complete PRG (load address + body) via defMON's SAVE
        encoder (the faithful ``$D626`` walk). When LOADed, writes the song
        into RAM at ``$1800..decode_top``. Applies the save-prep passes —
        ``$11`` position markers and zeroed pattern-pointer table — over the
        encode range, and keeps the source file's post-LOAD gates, so
        defMON's LOAD runs (and clears) exactly the passes the source
        elected. An unmodified load round-trips byte-identical to the
        original file.
        """
        snap = bytearray(self.snapshot)
        off_lo = 0x1800 - LOAD_ADDRESS  # song_position_lo
        off_hi = 0x1900 - LOAD_ADDRESS  # song_position_hi
        off_pat = 0x1A00 - LOAD_ADDRESS  # pat_base_lo/hi
        top = self.decode_top
        for x in range(256):
            if snap[off_hi + x] != 0:
                snap[off_lo + x] = 0x11
                snap[off_hi + x] = 0x11
        for x in range(256):
            snap[off_pat + x] = 0
        src_len = top - LOAD_ADDRESS + 1
        try:
            return encode_worktune(bytes(snap[:src_len]), top)
        except CodecError as e:
            raise DefmonError(f"SAVE-encode failed: {e}") from e

    def to_file(self, path: str) -> None:
        with open(path, "wb") as f:
            f.write(self.to_bytes())

    def to_replay(self, editor_image: bytes, *, raw: bool = False) -> bytes:
        """Pack this worktune into a playable ``$1000`` replay.

        Runs defMON's own SAVE pipeline (``pydefmon._packer``) on a
        user-supplied 65536-byte decrunched editor RAM image; defaults
        to PSID bytes, ``raw=True`` returns the packed ``.prg``.
        """
        if self.form != "editor":
            raise DefmonError(
                "to_replay packs a .prg worktune; this song already "
                "came from a PSID/RSID replay"
            )

        image, _end = _packer.pack(editor_image, self.image_bytes)
        if raw:
            return _packer.to_prg(image)

        return write_psid(
            load=_packer.INIT_ADDRESS,
            init=_packer.INIT_ADDRESS,
            play=_packer.PLAY_ADDRESS,
            image=image,
            name="defMON worktune",
            flags=0x20,  # 8580 chip, as defMON's own releases name it
        )

    # ---- convenience properties ----

    @property
    def load_address(self) -> int:
        return LOAD_ADDRESS

    @property
    def end_address(self) -> int:
        """Address one past the last snapshot byte at runtime."""
        return LOAD_ADDRESS + len(self.snapshot)

    @property
    def replay_layout(self) -> str:
        """The replay layout strategy that decoded this song (``"editor"`` /
        ``"goto80"``), or ``""`` for a workfile / from-scratch song."""
        return self.layout_entry.value if self.layout_entry else ""

    @property
    def variant(self) -> Variant:
        """The song's detected facets (container, replay layout, player signature
        and data base, post-LOAD gates), each with its source."""
        return detect_variant(self)

    @property
    def form(self) -> str:
        """Logical content form: ``"editor"`` (.prg / scratch) or ``"packed"`` (.sid).

        A ``.prg`` holds the composer's authoring form; a ``.sid`` is
        defMON's ``$D02C``-packed canonical form (renumbered patterns/sidTAB
        rows, merged empty steps, tempo zeroed). Use :meth:`diff` to compare.
        """
        return "packed" if self.replay_layout else "editor"

    # ---- region accessors ----

    def _region(self, addr: int, size: int) -> memoryview:
        if addr < LOAD_ADDRESS:
            raise DefmonError(f"region ${addr:04X} below load address")
        offset = addr - LOAD_ADDRESS
        end = min(offset + size, len(self.snapshot))
        return memoryview(self.snapshot)[offset:end]

    @property
    def song_position_arrays(self) -> memoryview:
        """``$1800..$19FF`` (512 bytes). Per-song-step pointer cache;
        defMON's saver leaves ``$11`` markers wherever the position is
        active; $CF42 (at LOAD, when gate ``$715E`` is non-zero) computes
        the runtime pointers.
        ``unpacked_snapshot()`` reconstructs the live values."""
        return self._region(0x1800, 0x200)

    @property
    def pattern_pointer_table(self) -> memoryview:
        """``$1A00..$1AFF`` (256 bytes). Zeroed by defMON on save;
        ``$D004`` fills it at LOAD only when gate ``$715F`` is non-zero
        (older editors' saves leave it zero after LOAD).
        ``unpacked_snapshot()`` computes ``$1A00,X``/``$1A80,X`` =
        low/high of ``$1F00 + X*$80``."""
        return self._region(0x1A00, 0x100)

    @property
    def voice_pattern_refs(self) -> memoryview:
        """``$1B00..$1DFF`` (768 bytes) + ``$1E00..$1EFF`` (256 bytes,
        DL). Spans two unrelated regions glued together for legacy
        convenience; prefer :attr:`arranger_v1`, :attr:`arranger_v2`,
        :attr:`arranger_v3`, and :attr:`sidtab_dl` (or
        :attr:`sidtab_jp`) for new code. The SID#2 (stereo) arrangers
        live separately at ``$6E00..$70FF`` (see :attr:`arranger_v4`,
        :attr:`arranger_v5`, :attr:`arranger_v6`) and are out of this
        view.

        Indexed by **song-step** Y for the arrangers:

        * ``$1B00,Y`` / ``$1C00,Y`` / ``$1D00,Y`` -- V1, V2, V3 pattern
          indices to play at this step (0 = silent voice).

        Indexed by **sidTAB row** Y for the DL column:

        * ``$1E00,Y`` -- per-sidTAB-row delay/STop byte (DL column).
          ``$00..$7F`` = hold N+1 frames; ``$80..$FF`` = STop.
        """
        return self._region(0x1B00, 0x400)

    @property
    def arranger_v1(self) -> memoryview:
        """``$1B00..$1BFF`` -- V1 (SID#1 voice 0) per-song-step pattern
        number; 0 = silent voice on this step. Indexed by song-step Y.
        """
        return self._region(0x1B00, 0x100)

    @property
    def arranger_v2(self) -> memoryview:
        """``$1C00..$1CFF`` -- V2 (SID#1 voice 1) per-song-step pattern
        number; 0 = silent voice on this step. Indexed by song-step Y.
        """
        return self._region(0x1C00, 0x100)

    @property
    def arranger_v3(self) -> memoryview:
        """``$1D00..$1DFF`` -- V3 (SID#1 voice 2) per-song-step pattern
        number; 0 = silent voice on this step. Indexed by song-step Y.
        """
        return self._region(0x1D00, 0x100)

    @property
    def sidtab_jp(self) -> memoryview:
        """``$1900..$19FF`` (256 bytes). Per-sidTAB-row JP marker (raw
        on-disk form, pre-``$CF42``). Indexed by sidTAB row index Y.

        * ``$11`` -- ACTIVE LINEAR: row Y is active; cascade advances to
          row Y+1 after DL underflow. ``$CF42`` rewrites this to ``$5F``
          (runtime pointer-hi) at LOAD time when gate ``$715E`` is non-zero;
          the paired ``$1800,Y`` is rewritten to ``Y * $0F`` (pointer-lo).
        * ``$00`` -- INACTIVE / JUMP-SOURCE: cascade does not advance
          linearly to this row. ``$1800,Y`` (paired) holds the JP
          target row index that the player jumps to instead. ``$CF42``
          leaves both bytes alone.

        See :meth:`set_jp` / :meth:`jp_target` for the high-level API.
        :meth:`unpacked_snapshot` returns a buffer in the post-``$CF42``
        form (runtime pointers in place of the ``$11`` markers).
        """
        return self._region(0x1900, 0x100)

    @property
    def sidtab_dl(self) -> memoryview:
        """``$1E00..$1EFF`` (256 bytes). Per-sidTAB-row DL byte.
        Indexed by sidTAB row index Y.

        * ``$00..$7F`` -- HOLD: cascade holds this row for ``N + 1``
          frames, then advances per :attr:`sidtab_jp` semantics.
        * ``$80..$FF`` -- STop: cascade freezes on this row until a
          :class:`PatternEvent` with GATE_A / GATE_B re-arms the slot.

        See :meth:`set_dl` for the high-level setter.
        """
        return self._region(0x1E00, 0x100)

    @property
    def pattern_bodies(self) -> memoryview:
        """``$1F00..$5EFF`` (16384 bytes). 128 pattern bodies of 128
        bytes each (32 events x 4 bytes)."""
        return self._region(0x1F00, 0x4000)

    def pattern(self, index: int) -> memoryview:
        """One 128-byte pattern body, ``0 <= index < 128``."""
        if not 0 <= index < 128:
            raise IndexError(f"pattern index {index} out of range [0, 128)")
        return self._region(0x1F00 + index * 0x80, 0x80)

    @property
    def sidtab_region(self) -> memoryview:
        """``$5F00..$7166``. 256 sidTAB rows (15 bytes each,
        ``$5F00..$6DFF``; see ``sidtab_row()``), then the SID#2 stereo
        arrangers (:attr:`arranger_v4` / ``v5`` / ``v6`` at
        ``$6E00``/``$6F00``/``$7000``), then the runtime-state page
        (``$7100..$7166``: post-LOAD gates, tempo, sub-frame count,
        stereo flag, SID#2 base — see
        :attr:`cia2_timer` / :attr:`sub_frame_count` /
        :attr:`stereo_enable` / :attr:`sid2_base`). Sidcall ``N``
        starts at row ``N`` and runs through subsequent rows until a
        freeze or loop terminator -- see ``sidcall_frames()``.
        """
        return self._region(0x5F00, 0x7167 - 0x5F00)

    @property
    def arranger_v4(self) -> memoryview:
        """``$6E00..$6EFF`` -- SID#2 voice-4 (V3) per-song-step pattern
        number; 0 = silent voice on this step. Indexed by song-step Y.
        Read by the player only when stereo is enabled
        (:attr:`stereo_enable`)."""
        return self._region(0x6E00, 0x100)

    @property
    def arranger_v5(self) -> memoryview:
        """``$6F00..$6FFF`` -- SID#2 voice-5 (V4) per-song-step pattern
        number; 0 = silent voice on this step. Indexed by song-step Y.
        Read by the player only when stereo is enabled
        (:attr:`stereo_enable`)."""
        return self._region(0x6F00, 0x100)

    @property
    def arranger_v6(self) -> memoryview:
        """``$7000..$70FF`` -- SID#2 voice-6 (V5) per-song-step pattern
        number; 0 = silent voice on this step. Indexed by song-step Y.
        Read by the player only when stereo is enabled
        (:attr:`stereo_enable`)."""
        return self._region(0x7000, 0x100)

    # ---- runtime-state page ($7100..$7166): per-tune playback fields ----

    @property
    def cia2_timer(self) -> int:
        """``$715A``/``$715B`` -- the 16-bit CIA2 Timer-A reload interval;
        the per-tune tempo (NMI period). Combined with
        :attr:`sub_frame_count`, the main-tick rate is
        ``cia2_timer × sub_frame_count`` cycles per player advance."""
        lo = self.snapshot[CIA2_TIMER_LO]
        hi = self.snapshot[CIA2_TIMER_HI]
        return lo | (hi << 8)

    @property
    def sub_frame_count(self) -> int:
        """``$715C`` -- NMIs per main player tick: 1, 2, 4, or 8."""
        return self.snapshot[SUB_FRAME_COUNT]

    @property
    def stereo_enable(self) -> bool:
        """``$715D`` -- stereo (2SID) mode flag. Off: the SID#2
        arrangers are dead data and the player drives voices 1-3 only."""
        return self.snapshot[STEREO_ENABLE] != 0

    @property
    def sid2_base(self) -> int:
        """``$7164``/``$7165`` -- the SID#2 register base as a 16-bit word
        (``lo | hi << 8``); the legal mappings are ``$D4xx``/``$D5xx``/
        ``$DExx``/``$DFxx`` with the low byte a ``$20`` multiple."""
        lo = self.snapshot[SID2_BASE_LO]
        hi = self.snapshot[SID2_BASE_HI]
        return lo | (hi << 8)

    def _set16(self, lo_off: int, value: int) -> None:
        self.snapshot[lo_off] = value & 0xFF
        self.snapshot[lo_off + 1] = (value >> 8) & 0xFF

    def set_cia2_timer(self, value: int) -> None:
        """Set the per-tune tempo word at ``$715A``/``$715B``
        (1 = one CIA tick, up to 65535; 0 underflows the timer)."""
        if not 1 <= value <= 0xFFFF:
            raise ValueError(f"CIA2 timer must be 1..65535, got {value}")
        self._set16(CIA2_TIMER_LO, value)

    def set_sub_frame_count(self, value: int) -> None:
        """Set ``$715C`` (NMIs per main tick); must be 1, 2, 4, or 8."""
        if value not in (1, 2, 4, 8):
            raise ValueError(f"sub-frame count must be 1, 2, 4, or 8, got {value}")
        self.snapshot[SUB_FRAME_COUNT] = value

    def set_stereo(self, enable: bool) -> None:
        """Set the stereo (2SID) flag at ``$715D``."""
        self.snapshot[STEREO_ENABLE] = 1 if enable else 0

    def set_sid2_base(self, word: int) -> None:
        """Set the SID#2 register base at ``$7164``/``$7165``.

        ``word`` is ``lo | hi << 8``; the legal mappings are
        ``$D4xx``/``$D5xx``/``$DExx``/``$DFxx`` with the low byte one of
        ``$00``/``$20``/``$40``/``$60``/``$80``/``$A0``/``$C0``/``$E0``.
        """
        hi = (word >> 8) & 0xFF
        lo = word & 0xFF
        if hi not in (0xD4, 0xD5, 0xDE, 0xDF):
            raise ValueError(
                f"SID#2 base hi must be one of $D4/$D5/$DE/$DF, got ${hi:02X}"
            )
        if lo & 0x1F != 0 or lo > 0xE0:
            raise ValueError(
                f"SID#2 base lo must be a $20 multiple of $00..$E0, got ${lo:02X}"
            )
        self._set16(SID2_BASE_LO, lo | (hi << 8))

    # ---- builders for from-scratch tune authoring ----

    def clear_arranger(self) -> None:
        """Zero all six per-step pattern-reference arrays:
        ``$1B00..$1DFF`` (SID#1 voices 1-3) and ``$6E00..$70FF``
        (SID#2 voices 4-6, the stereo arrangers).

        Use this when you want to reset the arrangement without
        also wiping the sidTAB DL byte region — see :meth:`clear_dl`
        for that. :meth:`clear_song_table` calls both, in the right
        order, and is provided for callers that want the legacy
        all-in-one wipe.
        """
        for offset in (0x1B00, 0x1C00, 0x1D00, 0x6E00, 0x6F00, 0x7000):
            base = offset - LOAD_ADDRESS
            for i in range(0x100):
                self.snapshot[base + i] = 0

    def clear_dl(self) -> None:
        """Zero the per-sidTAB-row DL byte region at ``$1E00..$1EFF``.

        DL bytes control cascade hold timing and STop. Wiping them
        means every active row holds for exactly 1 frame and never
        STops — typically *not* what you want unless you are about
        to repopulate the DL bytes with :meth:`set_dl`.
        """
        base = 0x1E00 - LOAD_ADDRESS
        for i in range(0x100):
            self.snapshot[base + i] = 0

    def clear_song_table(self) -> None:
        """Wipe ``voice_pattern_refs`` to all zero — clears BOTH the
        arranger arrays (``$1B00..$1DFF`` and the SID#2 stereo
        arrangers ``$6E00..$70FF``) AND the sidTAB DL byte region at
        ``$1E00..$1EFF``.

        **Gotcha**: this wipes the DL bytes too, so it must run
        **before** any :meth:`set_dl` calls (and before populating
        sidTAB rows that rely on a non-zero DL). Running it after
        ``set_dl`` silently zeros every DL byte and the cascade
        refetches every frame instead of holding rows.

        Prefer the more targeted :meth:`clear_arranger` and
        :meth:`clear_dl` when you only need one or the other.
        """
        self.clear_arranger()
        self.clear_dl()

    def set_step(
        self,
        y: int,
        *,
        v1: int = 0,
        v2: int = 0,
        v3: int = 0,
        v4: "int | None" = None,
        v5: "int | None" = None,
        v6: "int | None" = None,
    ) -> None:
        """Set song step ``y`` to play patterns on the six voices.

        ``v1``/``v2``/``v3`` (SID#1 voices 1-3) default to 0 (silent
        voice). ``v4``/``v5``/``v6`` (SID#2 voices 4-6, stereo) default
        to ``None`` = leave those arranger bytes untouched, so editing
        a mono step never clobbers stereo arranger data. Pattern
        indices must be 0-127. To create a jump entry, call
        ``set_jump`` instead.
        """
        if not 0 <= y <= 255:
            raise ValueError(f"song step must be 0..255, got {y}")
        for name, val in (
            ("v1", v1),
            ("v2", v2),
            ("v3", v3),
            ("v4", v4),
            ("v5", v5),
            ("v6", v6),
        ):
            if val is not None and not 0 <= val <= 0x7F:
                raise ValueError(f"{name} pattern index must be 0..127, got {val}")
        OFF_1B = 0x1B00 - LOAD_ADDRESS
        OFF_1C = 0x1C00 - LOAD_ADDRESS
        OFF_1D = 0x1D00 - LOAD_ADDRESS
        self.snapshot[OFF_1B + y] = v1
        self.snapshot[OFF_1C + y] = v2
        self.snapshot[OFF_1D + y] = v3
        for arr, val in (
            (0x6E00, v4),
            (0x6F00, v5),
            (0x7000, v6),
        ):
            if val is not None:
                self.snapshot[arr - LOAD_ADDRESS + y] = val

    def set_jump(
        self, y: int, *, target: int, count: int = 0, stereo: bool = False
    ) -> None:
        """Mark song step ``y`` as a jump command. The song will jump
        to step ``target`` ``count`` times before falling through.
        ``count == 0`` is unconditional (infinite loop).

        On disk: ``$1B00,y = $FF`` (the arranger's jump marker),
        with ``$1C00,y = target`` and ``$1D00,y = count``. Each chip's
        arranger walker is independent, so a stereo song must carry the
        same jump row in the SID#2 arrangers too; pass
        ``stereo=True`` to mark that one as well (``$6E00,y = $FF``,
        ``$6F00,y = target``, ``$7000,y = count``).
        """
        if not 0 <= y <= 255:
            raise ValueError(f"song step must be 0..255, got {y}")
        if not 0 <= target <= 255:
            raise ValueError(f"jump target must be 0..255, got {target}")
        if not 0 <= count <= 255:
            raise ValueError(f"loop count must be 0..255, got {count}")
        OFF_1B = 0x1B00 - LOAD_ADDRESS
        OFF_1C = 0x1C00 - LOAD_ADDRESS
        OFF_1D = 0x1D00 - LOAD_ADDRESS
        self.snapshot[OFF_1B + y] = 0xFF
        self.snapshot[OFF_1C + y] = target
        self.snapshot[OFF_1D + y] = count
        if stereo:
            for arr, val in ((0x6E00, 0xFF), (0x6F00, target), (0x7000, count)):
                self.snapshot[arr - LOAD_ADDRESS + y] = val

    # ---- sidTAB row metadata (JP / DL) ----

    def set_dl(self, row: int, value: int) -> None:
        """Set the DL byte at ``$1E00,row`` for sidTAB row ``row``
        (0..255). ``value`` 0..127 = hold N+1 frames; 128..255 = STop
        (freeze until re-armed)."""
        if not 0 <= row <= 255:
            raise ValueError(f"sidTAB row must be 0..255, got {row}")
        if not 0 <= value <= 0xFF:
            raise ValueError(f"DL value must be 0..255, got {value}")
        self.snapshot[0x1E00 - LOAD_ADDRESS + row] = value

    def set_jp(self, row: int, target: "int | None" = None) -> None:
        """Configure JP for sidTAB row ``row`` (0..255).

        ``target=None`` -- mark this row ACTIVE LINEAR. Writes
        ``$1900,row = $11`` and leaves ``$1800,row`` alone. ``$CF42``
        rewrites both bytes at LOAD time to ``$5F00 + row * $0F``.

        ``target=K`` -- mark this row as a JUMP-SOURCE pointing at
        row ``K`` (0..255). Writes ``$1900,row = $00`` and
        ``$1800,row = K``. ``$CF42`` skips both bytes (its ``BEQ`` on
        ``$1900,X`` short-circuits the rewrite); the cascade reads
        ``$1900,row == 0`` and jumps to row ``K`` via the
        ``$1800,row`` pointer-lo target.
        """
        if not 0 <= row <= 255:
            raise ValueError(f"sidTAB row must be 0..255, got {row}")
        off_1800 = 0x1800 - LOAD_ADDRESS + row
        off_1900 = 0x1900 - LOAD_ADDRESS + row
        if target is None:
            self.snapshot[off_1900] = 0x11
        else:
            if not 0 <= target <= 255:
                raise ValueError(f"JP target must be 0..255, got {target}")
            self.snapshot[off_1800] = target & 0xFF
            self.snapshot[off_1900] = 0x00

    def jp_target(self, row: int) -> "int | None":
        """Read the JP target for sidTAB row ``row``.

        Returns ``None`` if the row is active linear
        (``$1900,row == $11``), the JP target row index if the row is
        a jump-source (``$1900,row == $00``), or ``None`` for inactive
        slots (both bytes zero). Reads the on-disk marker form; not
        valid after :meth:`unpacked_snapshot` has been applied."""
        if not 0 <= row <= 255:
            raise ValueError(f"sidTAB row must be 0..255, got {row}")
        hi = self.snapshot[0x1900 - LOAD_ADDRESS + row]
        if hi == 0x11:
            return None
        if hi == 0x00:
            lo = self.snapshot[0x1800 - LOAD_ADDRESS + row]
            if lo == 0:
                return None  # inactive slot (both zero)
            return lo
        return None  # already-unpacked or unrecognised marker

    def pattern_events(self, index: int) -> list["PatternEvent"]:
        """Decode pattern ``index`` as 32 four-byte ``PatternEvent``s."""
        raw = bytes(self.pattern(index))
        return [PatternEvent.parse(raw[i * 4 : i * 4 + 4]) for i in range(32)]

    def set_pattern_events(self, index: int, events) -> None:
        """Write exactly 32 ``PatternEvent``s into pattern ``index``."""
        events = list(events)
        if len(events) != 32:
            raise ValueError(f"pattern needs exactly 32 events, got {len(events)}")
        raw = b"".join(ev.to_bytes() for ev in events)
        p = self.pattern(index)
        for i, b in enumerate(raw):
            p[i] = b

    def sidcall_frames(
        self, start_y: int, max_frames: int = 256
    ) -> list["SidcallFrame"]:
        """Walk the sidcall starting at sidtab row ``start_y``.

        Returns a list of ``SidcallFrame`` covering the rows the player
        fetches from ``start_y`` (row 255 wraps to 0, a JP redirects one
        level) until a freeze frame (DL ``$80+``), a JP onto a JP, a loop
        back to a visited row, or ``max_frames``.
        """
        unpacked = self.unpacked_snapshot()
        sidtab = bytes(self.sidtab_region)
        OFF_1800 = 0x0000
        OFF_1900 = 0x0100
        OFF_1E00 = 0x0600

        frames: list[SidcallFrame] = []
        seen: set[int] = set()
        y = start_y & 0xFF
        while len(frames) < max_frames:
            if unpacked[OFF_1900 + y] == 0:
                y = unpacked[OFF_1800 + y]
                if unpacked[OFF_1900 + y] == 0:
                    break  # the player would take its row pointer from page zero
            if y in seen:
                if frames:
                    frames[-1].loops_to = y
                break
            seen.add(y)
            row_bytes = sidtab[y * 15 : y * 15 + 15]
            control = unpacked[OFF_1E00 + y]
            # Pass the raw on-disk JP marker (from self.snapshot, not the
            # unpacked form -- unpacked_snapshot rewrites $11 -> runtime
            # pointer-hi). dl == control by definition.
            jp_marker = self.snapshot[0x1900 - LOAD_ADDRESS + y]
            row = SidtabRow.parse(y, bytes(row_bytes), jp=jp_marker, dl=control)
            frames.append(SidcallFrame(y, row, control))
            if control & 0x80:
                break
            y = (y + 1) & 0xFF
        return frames

    # ---- whole-song optimization --------------------------------------

    def optimize(self) -> "DefmonSong":
        """In-place shrink the song's resource use without changing
        the per-frame SID register stream the player produces.

        Currently does two safe transforms:

        * **Sidtab row dedup.** STop'd rows (``$1E00,row >= $80``)
          with identical raw bytes + ``jp`` marker are merged. Pattern
          events whose ``slot_a`` / ``slot_b`` references a duplicate
          row are rewritten to point at the lowest-indexed copy. The
          duplicate row's bytes are left in place — only references
          are repointed — which is why non-STop rows are skipped: they
          may be walked into as interior rows of a longer cascade
          where index matters.
        * **Pattern dedup.** Pattern bodies whose 128-byte image is
          identical are merged. Arranger entries
          (``$1B00`` / ``$1C00`` / ``$1D00,y``) referencing a duplicate
          pattern are rewritten to the canonical (lowest-indexed) copy.
          When stereo is enabled (:attr:`stereo_enable`), the SID#2
          arrangers (``$6E00`` / ``$6F00`` / ``$7000,y``) are rewritten
          the same way — mirroring defMON's own packer, which only
          touches them when the stereo flag is set. Pattern 0 is
          reserved as the "silent voice on this step" target and is
          never used as a remap destination; a row whose V1 (or V4) entry
          has bit 7 set is a jump command and is skipped entirely
          because its V2/V3 (or V5/V6) bytes are target/count rather
          than pattern indices.

        Idempotent — running twice produces the same song. Returns
        ``self`` so calls chain (``song.optimize().to_file(...)``).
        """
        self._dedup_sidtab_rows()
        self._dedup_patterns()
        return self

    def _dedup_sidtab_rows(self) -> None:
        # Only STop'd rows (dl >= 0x80) are safe to repoint: a non-STop
        # row's cascade advances to (row + 1) or via JP, so the index
        # itself is load-bearing.
        canonical: dict = {}
        remap: dict = {}
        for row_idx in range(1, 256):  # row 0 reserved as a silent sentinel
            sr = self.sidtab_row(row_idx)
            if sr.dl is None or sr.dl < 0x80:
                continue
            key = (sr.to_bytes(), sr.dl, sr.jp)
            if key in canonical:
                remap[row_idx] = canonical[key]
            else:
                canonical[key] = row_idx
        if not remap:
            return
        for pat_idx in range(128):
            events = self.pattern_events(pat_idx)
            changed = False
            for ev in events:
                if ev.gate_a and ev.slot_a in remap:
                    ev.slot_a = remap[ev.slot_a]
                    changed = True
                if ev.gate_b and ev.slot_b in remap:
                    ev.slot_b = remap[ev.slot_b]
                    changed = True
            if changed:
                self.set_pattern_events(pat_idx, events)

    def _dedup_patterns(self) -> None:
        canonical: dict = {}
        remap: dict = {}
        # Start at 1: pattern 0 is the "silent voice" target and must
        # never be the remap destination (a $00 V0 entry means "silent",
        # not "play pattern 0").
        for pat_idx in range(1, 128):
            body = bytes(self.pattern(pat_idx))
            if body in canonical:
                remap[pat_idx] = canonical[body]
            else:
                canonical[body] = pat_idx
        if not remap:
            return
        # defMON's own packer rewrites the SID#2 arrangers only while stereo is enabled; mirror that gate
        groups = [
            (
                (0x1B00 - LOAD_ADDRESS, 0x1C00 - LOAD_ADDRESS, 0x1D00 - LOAD_ADDRESS),
                0x1B00 - LOAD_ADDRESS,
            ),
        ]
        if self.snapshot[STEREO_ENABLE]:
            groups.append(
                (
                    (
                        0x6E00 - LOAD_ADDRESS,
                        0x6F00 - LOAD_ADDRESS,
                        0x7000 - LOAD_ADDRESS,
                    ),
                    0x6E00 - LOAD_ADDRESS,
                )
            )
        for idx in range(256):
            for triple, jump_row in groups:
                if self.snapshot[jump_row + idx] & 0x80:
                    # jump row: V1/V2 (or V4/V5) are target/count, not pattern indices
                    continue
                for arr_base in triple:
                    val = self.snapshot[arr_base + idx]
                    if val == 0:
                        continue  # silent voice on this step
                    pat_num = val & 0x7F
                    if pat_num in remap:
                        self.snapshot[arr_base + idx] = (val & 0x80) | remap[pat_num]

    def sidtab_row(self, index: int) -> "SidtabRow":
        """Decode the 15-byte sidtab row at index ``0 <= index < 256``.

        The returned ``SidtabRow`` carries its co-addressed ``jp`` byte
        (read from ``$1900,index`` on disk) and ``dl`` byte
        (``$1E00,index``) as companion attributes. See
        :class:`SidtabRow` for byte semantics; reads the on-disk
        marker form (``unpacked_snapshot`` rewrites those bytes)."""
        if not 0 <= index < 256:
            raise IndexError(f"sidtab row {index} out of range [0, 256)")
        raw = bytes(self._region(0x5F00 + index * 15, 15))
        jp = self.snapshot[0x1900 - LOAD_ADDRESS + index]
        dl = self.snapshot[0x1E00 - LOAD_ADDRESS + index]
        return SidtabRow.parse(index, raw, jp=jp, dl=dl)

    def diff(self, other: "DefmonSong") -> list[RegionDiff]:
        """Region-wise byte comparison against another song, in ``DIFF_REGIONS`` order.

        Compares stored snapshots over the named regions tiling
        ``$1800..$7166``. For a ``.prg`` vs its matching ``.sid`` (same tune,
        different form) it reports which regions agree and which differ.
        """
        a, b = self.snapshot, other.snapshot
        out = []
        for name, base, size in DIFF_REGIONS:
            off = base - LOAD_ADDRESS
            differing = sum(1 for i in range(size) if a[off + i] != b[off + i])
            out.append(RegionDiff(name, base, size, differing))
        return out

    def unpacked_snapshot(self) -> bytearray:
        """Return the snapshot in post-LOAD form, with defMON's three gated passes.

        defMON's ``post_load_chain`` ($CEE4..$CFEE) runs each pass only when
        its gate byte is non-zero, and each pass that runs clears its gate:

        * gate ``$715E`` → ``$CF42`` rewrites active (``$1900,X != 0``) rows to
          ``$5F00 + X*$0F`` pointers;
        * gate ``$715F`` → ``$D004`` fills ``$1A00,X``/``$1A80,X`` with
          ``$1F00 + X*$80``;
        * gate ``$7162`` → ``$CFB8`` sets bit 7 of every pattern's step-31 byte
          (offset ``$7C``).

        A file saved by an editor without a pass carries that gate zero and
        keeps its raw decoded bytes there after LOAD (e.g. V20140612 saves
        ``$715F`` zero, so LOAD leaves ``$1A00..$1AFF`` zero).
        """
        out = bytearray(self.snapshot)
        if out[GATE_SONG_POSITION] != 0:
            OFF_1800 = 0x1800 - LOAD_ADDRESS
            OFF_1900 = 0x1900 - LOAD_ADDRESS
            addr = 0x5F00
            for x in range(256):
                if out[OFF_1900 + x] != 0:
                    out[OFF_1800 + x] = addr & 0xFF
                    out[OFF_1900 + x] = (addr >> 8) & 0xFF
                addr += 0x0F
            out[GATE_SONG_POSITION] = 0
        if out[GATE_PAT_BASE] != 0:
            OFF_1A00 = 0x1A00 - LOAD_ADDRESS
            OFF_1A80 = 0x1A80 - LOAD_ADDRESS
            for x in range(128):
                ptr = 0x1F00 + x * 0x80
                out[OFF_1A00 + x] = ptr & 0xFF
                out[OFF_1A80 + x] = (ptr >> 8) & 0xFF
            out[GATE_PAT_BASE] = 0
        if out[GATE_PATTERN_MARKER] != 0:
            OFF_1F = 0x1F00 - LOAD_ADDRESS
            for x in range(128):
                out[OFF_1F + x * 0x80 + 0x7C] |= 0x80
            out[GATE_PATTERN_MARKER] = 0
        return out

    def __repr__(self) -> str:
        return f"DefmonSong(snapshot={len(self.snapshot)} bytes)"


class SidtabRow:
    """One sidTAB row — defMON's per-frame envelope/timbre program.

    The cascade walks sidTAB rows for each voice while a note is
    playing; each row contributes zero or more column overrides
    (waveform, ADSR, pulse width, filter, slide direction,
    transpose) that apply to the voice on the frame the cascade
    fetches the row, then the row's ``dl`` byte determines how many
    frames the cascade holds before fetching the next.

    Each row in the snapshot is 15 bytes at ``$5F00 + index * $0F``:

    * byte 0 — low-half bitmap: which of ``WGh / WGl / AD / SR /
      TR / AF / PW`` are present in this row (bit per column).
    * bytes 1..N — values for the bits that were set in the low
      bitmap, in screen-column order.
    * byte 1+N — high-half bitmap: which of ``PS / RE / FV / CP /
      ACID`` are present.
    * bytes 2+N..M — values for the high half. All are 1 byte
      except ``ACID`` (2 bytes).
    * remaining bytes — zero pad.

    A column attribute on the parsed :class:`SidtabRow` is
    ``None`` when its bit isn't set in the bitmap (= "no override
    on this row, inherit the prior value"). When a bit IS set, the
    column value is what the cascade will apply when this row
    fetches.

    Column → effect mapping:

    =====  ============  ================================================
    Col    SID reg       Effect
    =====  ============  ================================================
    WGh    $D404         Waveform nibble; the voice emits ``WGh ^ WGl``.
    WGl    $D404         Gate / sync / ring / test nibble, set apart from
                          WGh so a row can gate without changing waveform.
    AD     $D405         Attack (high nibble) / decay (low nibble).
    SR     $D406         Sustain (high nibble) / release (low nibble).
    TR     (note byte)   New ``current_note``: bit 7 set = absolute
                          ``byte & $7F``, else ``(last GATE_N note + byte)
                          & $7F`` (older replays: 8-bit add, no mask).
    AF     (slide mode)  ``$00`` none, ``$01..$7F`` constant detune (not a
                          portamento), ``$80..$FF`` per-frame slide, bit 6
                          down (``docs/format.md`` Pitch slide).
    PW     $D402/$D403   Pulse width, nibbles swapped: ``$YX`` gives the
                          12-bit width ``$XY0``.
    PS     (depth byte)  Pulse-width sweep depth + direction (bit 7 =
                          direction). The sweep modulates ``pulse_lo``
                          each frame until clamped, then auto-reverses.
    RE     $D417         Resonance high nibble + voice-routing low
                          nibble. Composable via a 3-way dispatch
                          (set / OR-with-voice-mask / AND-clear).
    FV     $D418         Volume low nibble + filter-mode high nibble.
                          Emitted as ``(byte & $F0) | $0F`` (volume
                          always max).
    CP     $D416         Filter-cutoff hi delta — added to the cutoff
                          accumulator's saturation step each frame.
    ACID   $D416         16-bit cutoff-slide command: low byte = step
                          magnitude, high byte = direction + control
                          (bit 7 = "slide" vs "absolute"; bit 6 = SBC
                          vs ADC). Drives the cutoff slide oscillator.
                          Stored little-endian (step byte first), the
                          order the player consumes it in.
    =====  ============  ================================================

    The cascade also reads two companion bytes per row, stored in
    other RAM regions but addressed by the same row index. They're
    attached to the :class:`SidtabRow` instance as optional
    attributes (set when the row was fetched via
    :meth:`DefmonSong.sidtab_row`; ``None`` otherwise):

    * ``jp`` — ``$1900,index``. JP marker. ``$11`` = active linear
      (cascade advances to ``index + 1`` after DL underflow); ``$00``
      = JP source (cascade jumps to row ``$1800,index`` instead of
      advancing). Either way, the cascade's effective row is the one
      whose data this :class:`SidtabRow` was parsed from.
    * ``dl`` — ``$1E00,index``. Hold timing for this row.
      ``$00..$7F`` = hold for ``dl + 1`` frames then advance per the
      JP marker. ``$80..$FF`` = STop (freeze the cascade on this row
      until a ``PatternEvent`` re-arms it via GATE_A/B).
    """

    __slots__ = (
        "index",
        "raw",
        "low_bitmap",
        "high_bitmap",
        "WGh",
        "WGl",
        "AD",
        "SR",
        "TR",
        "AF",
        "PW",
        "PS",
        "RE",
        "FV",
        "CP",
        "ACID",
        "trailing",
        "jp",
        "dl",
    )

    def __init__(self, index: int, raw: bytes) -> None:
        self.index = index
        self.raw = raw
        self.low_bitmap = 0
        self.high_bitmap = 0
        self.WGh = self.WGl = self.AD = self.SR = self.TR = self.AF = self.PW = None
        self.PS = self.RE = self.FV = self.CP = self.ACID = None
        self.trailing = b""
        self.jp: "int | None" = None
        self.dl: "int | None" = None

    @classmethod
    def parse(
        cls,
        index: int,
        raw: bytes,
        *,
        jp: "int | None" = None,
        dl: "int | None" = None,
    ) -> "SidtabRow":
        """Decode the 15-byte ``raw`` row body. Optional ``jp`` and
        ``dl`` companion bytes are attached as-is (see class docstring
        for byte semantics)."""
        if len(raw) != 15:
            raise ValueError(f"sidtab row must be 15 bytes, got {len(raw)}")
        if jp is not None and not 0 <= jp <= 0xFF:
            raise ValueError(f"jp must be 0..255, got {jp}")
        if dl is not None and not 0 <= dl <= 0xFF:
            raise ValueError(f"dl must be 0..255, got {dl}")
        row = cls(index, raw)
        (row.low_bitmap, row.high_bitmap, columns), size = STREAMS.parse_prefix(
            raw, "row"
        )
        for name, value in columns:
            setattr(row, name, value)
        row.trailing = bytes(raw[size:])
        row.jp = jp
        row.dl = dl
        return row

    def to_bytes(self) -> bytes:
        """Inverse of :meth:`parse`: pack this row's columns + trailing
        bytes back into the 15-byte on-disk form."""
        out = bytearray(RowColumns.of(self.values()).to_bytes())
        out.extend(self.trailing)
        if len(out) > 15:
            raise ValueError(
                f"packed sidtab row exceeds 15 bytes ({len(out)}); "
                f"trailing length {len(self.trailing)} too long"
            )
        out.extend(b"\x00" * (15 - len(out)))
        return bytes(out)

    @classmethod
    def pack(cls, values: dict, trailing: bytes = b"") -> bytes:
        """Build a 15-byte row from a ``{column_name: value}`` dict +
        optional ``trailing`` bytes (zero-padded otherwise)."""
        row = cls(0, b"")
        for name, v in values.items():
            if name not in COLUMNS:
                raise ValueError(f"unknown sidtab column: {name!r}")
            setattr(row, name, int(v))
        row.trailing = bytes(trailing)
        return row.to_bytes()

    def values(self) -> dict:
        """Return ``{column_name: value}`` for columns with overrides set."""
        out = {}
        for name in COLUMNS:
            v = getattr(self, name)
            if v is not None:
                out[name] = v
        return out

    def __repr__(self) -> str:
        vals = ", ".join(
            f"{k}=${v:0{4 if v > 0xFF else 2}x}" for k, v in self.values().items()
        )
        anomaly = ""
        if any(b != 0 for b in self.trailing):
            anomaly = f" trailing={self.trailing.hex()}"
        companions = ""
        if self.jp is not None:
            companions += f" jp=${self.jp:02x}"
        if self.dl is not None:
            companions += f" dl=${self.dl:02x}"
        return f"SidtabRow(index={self.index}, {vals}{anomaly}{companions})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SidtabRow):
            return NotImplemented
        return (
            self.index == other.index
            and self.to_bytes() == other.to_bytes()
            and self.jp == other.jp
            and self.dl == other.dl
        )

    def __hash__(self) -> int:
        return hash((self.index, self.to_bytes(), self.jp, self.dl))


class PatternEvent:
    """One 4-byte step inside a pattern body — defMON's per-voice
    "note + which envelope to run" event.

    A pattern is a sequence of 32 events; voices walk independently
    over their assigned pattern (via the arrangers) at a cadence
    determined by each event's ``duration`` nibble. When the player
    reaches an event, it inspects the four flag bits to decide what
    to commit:

    Layout::

      byte 0  flag — bits 7..0 = ALT, GATE_A, GATE_B, GATE_N, duration[3..0]
      byte 1  slot_a — sidTAB row to arm for sidcall layer 1 (if GATE_A)
      byte 2  slot_b — sidTAB row to arm for sidcall layer 2 (if GATE_B)
      byte 3  note   — note byte to apply (if GATE_N). 1..120; 0 = no note.

    Flag bits (mask constants on the class):

    * ``FLAG_ALT`` (``$80``) — ends the arranger step for every
      voice, not just this one: the step lasts until its voices'
      earliest ALT. Its ``duration`` is loaded into all voices'
      counters, so the next patterns start ``duration + 2`` ticks later.
    * ``FLAG_GATE_A`` (``$40``) — arm sidcall layer 1 at row
      ``slot_a`` with counter 0; the row is fetched in the same frame.
    * ``FLAG_GATE_B`` (``$20``) — same for sidcall layer 2 via
      ``slot_b``; layer 2 applies after layer 1, so it wins a column
      both write.
    * ``FLAG_GATE_N`` (``$10``) — apply ``note`` as the voice's new
      ``current_note`` AND store the same byte in the voice's
      transpose buffer (the latter is the base for subsequent
      sidTAB TR-relative shifts). Also zeros the voice's
      slide-accumulator + slide-mode so the new note starts clean.
    * bits 3..0 — ``duration``: the step lasts ``duration + 2`` main
      ticks (fetched when the counter underflows, applied the tick
      after), so a step that gates nothing is pure delay.

    Build canonical events via the factories: :meth:`note_on`,
    :meth:`delay`, :meth:`alt_end`, :meth:`silent_pattern`. Edit
    fields directly for ad-hoc tweaks (they're mutable) and write
    back via :meth:`DefmonSong.set_pattern_events`.
    """

    FLAG_ALT = 0x80
    FLAG_GATE_A = 0x40
    FLAG_GATE_B = 0x20
    FLAG_GATE_N = 0x10
    FLAG_DURATION_MASK = 0x0F

    __slots__ = ("flag", "slot_a", "slot_b", "note")

    def __init__(self, flag: int, slot_a: int, slot_b: int, note: int) -> None:
        self.flag = flag & 0xFF
        self.slot_a = slot_a & 0xFF
        self.slot_b = slot_b & 0xFF
        self.note = note & 0xFF

    @classmethod
    def parse(cls, raw: bytes) -> "PatternEvent":
        if len(raw) != 4:
            raise ValueError(f"PatternEvent needs 4 bytes, got {len(raw)}")
        return cls(raw[0], raw[1], raw[2], raw[3])

    @classmethod
    def note_on(
        cls, note: int, *, slot_a: int = 0, slot_b: int = 0, duration: int = 0
    ) -> "PatternEvent":
        """Build a note-on event: gate the note (and optional sidcall
        slots) and set the duration."""
        if not 1 <= note <= 0xFF:
            raise ValueError(f"note byte must be 1..255, got ${note:02X}")
        if not 0 <= duration <= 0x0F:
            raise ValueError(f"duration must be 0..15, got {duration}")
        flag = cls.FLAG_GATE_N | (duration & 0x0F)
        if slot_a:
            flag |= cls.FLAG_GATE_A
        if slot_b:
            flag |= cls.FLAG_GATE_B
        return cls(flag, slot_a, slot_b, note)

    @classmethod
    def delay(cls, duration: int = 1) -> "PatternEvent":
        """Build a pure-delay event: no gates fired, a ``duration + 2``
        tick wait before the next event."""
        if not 0 <= duration <= 0x0F:
            raise ValueError(f"duration must be 0..15, got {duration}")
        return cls(duration & 0x0F, 0, 0, 0)

    @classmethod
    def alt_end(cls, duration: int = 0) -> "PatternEvent":
        """Build a pattern-end / song-advance trigger (alt-event).
        Place at event 31 to cleanly hand off to the next song step."""
        if not 0 <= duration <= 0x0F:
            raise ValueError(f"duration must be 0..15, got {duration}")
        return cls(cls.FLAG_ALT | (duration & 0x0F), 0, 0, 0)

    @classmethod
    def silent_pattern(cls) -> list["PatternEvent"]:
        """Build a 32-event silent pattern: 31 delays + alt-end."""
        return [cls.delay(1) for _ in range(31)] + [cls.alt_end()]

    def to_bytes(self) -> bytes:
        return bytes([self.flag, self.slot_a, self.slot_b, self.note])

    @property
    def alt(self) -> bool:
        return bool(self.flag & self.FLAG_ALT)

    @property
    def gate_a(self) -> bool:
        return bool(self.flag & self.FLAG_GATE_A)

    @property
    def gate_b(self) -> bool:
        return bool(self.flag & self.FLAG_GATE_B)

    @property
    def gate_n(self) -> bool:
        return bool(self.flag & self.FLAG_GATE_N)

    @property
    def duration(self) -> int:
        return self.flag & self.FLAG_DURATION_MASK

    _NOTE_NAMES = (
        "C-",
        "C#",
        "D-",
        "D#",
        "E-",
        "F-",
        "F#",
        "G-",
        "G#",
        "A-",
        "A#",
        "B-",
    )

    def sid_freq_word(self) -> "int | None":
        """Return the 16-bit SID freq word the player will latch for
        this event's note byte, or ``None`` for non-pitch values.

        Uses the same ``NOTE_PITCH_LO/HI`` tables the player walks
        (indexed by ``note`` directly — *not* ``note - 1``). The
        per-voice ``pitch_base`` detune (voices 1/2/3: +0/+1/+2 on
        freq_lo) is *not* applied here; callers that need a
        per-voice exact match should add it themselves.
        """
        if self.note == 0:
            return None
        return (NOTE_PITCH_HI[self.note] << 8) | NOTE_PITCH_LO[self.note]

    def frequency_hz(self) -> "float | None":
        word = self.sid_freq_word()
        if word is None:
            return None
        return word * PAL_CLOCK_HZ / (1 << 24)

    def note_name(self) -> str:
        if self.note == 0:
            return "---"
        n = self.note - 1
        return f"{self._NOTE_NAMES[n % 12]}{n // 12}"

    def __repr__(self) -> str:
        gates = (
            "".join(
                c
                for c, on in (
                    ("A", self.alt),
                    ("a", self.gate_a),
                    ("b", self.gate_b),
                    ("n", self.gate_n),
                )
                if on
            )
            or "-"
        )
        return (
            f"PatternEvent(flag=${self.flag:02X} [{gates}] dur={self.duration} "
            f"slot_a=${self.slot_a:02X} slot_b=${self.slot_b:02X} "
            f"note={self.note_name()})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PatternEvent):
            return NotImplemented
        return self.to_bytes() == other.to_bytes()

    def __hash__(self) -> int:
        return hash(self.to_bytes())


class SidcallFrame:
    """One frame of a sidcall: a sidtab row and its DL byte from
    ``$1E00,Y``. Returned by ``DefmonSong.sidcall_frames``.

    DL byte (``control``; ``dl`` is the canonical column name):

    * ``$00`` -- hold for 1 frame, then advance.
    * ``$01..$7F`` -- hold for ``control + 1`` frames, then advance.
    * ``$80..$FF`` -- STop: the slot stops advancing until a
      ``PatternEvent`` re-arms it.
    """

    __slots__ = ("row_index", "sidtab_row", "control", "loops_to")

    def __init__(self, row_index: int, sidtab_row: "SidtabRow", control: int) -> None:
        self.row_index = row_index
        self.sidtab_row = sidtab_row
        self.control = control & 0xFF
        self.loops_to: "int | None" = None

    @property
    def dl(self) -> int:
        """Alias for ``control`` using the on-spec column name."""
        return self.control

    @property
    def freezes(self) -> bool:
        return bool(self.control & 0x80)

    @property
    def hold_frames(self) -> "int | None":
        if self.freezes:
            return None
        return self.control + 1

    def __repr__(self) -> str:
        if self.freezes:
            timing = f"FREEZE (${self.control:02X})"
        elif self.loops_to is not None:
            timing = f"{self.hold_frames}f -> loop@Y={self.loops_to}"
        else:
            timing = f"{self.hold_frames}f"
        return (
            f"SidcallFrame(Y={self.row_index}, control=${self.control:02X} "
            f"[{timing}], {self.sidtab_row!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SidcallFrame):
            return NotImplemented
        return (
            self.row_index == other.row_index
            and self.control == other.control
            and bytes(self.sidtab_row.raw) == bytes(other.sidtab_row.raw)
        )

    def __hash__(self) -> int:
        return hash((self.row_index, self.control, bytes(self.sidtab_row.raw)))


class DefmonSidParser(BaseSidParser):
    """:class:`pysidtracker.BaseSidParser` adapter for defMON tunes.

    :meth:`parse` delegates to :meth:`DefmonSong.from_bytes`, which accepts
    both the ``.prg`` editor workfile and the PSID/RSID ``.sid`` replay
    container; the inherited :meth:`read` gives the shared
    path/bytes/file-like entry point.

    The ``.prg`` editor workfile carries no static signature, but the
    ``.sid`` replay does (defMON's player SID-write band), so
    :meth:`recognize` returns the signature site for a replay image --
    enabling the inherited :meth:`detect` to locate a defMON replay even
    when it is packed / relocated behind an init routine.
    """

    error_class: type = DefmonParseError

    def parse(
        self, data: bytes, subtune: "int | None" = None, **kwargs: Any
    ) -> DefmonSong:
        """The song of ``data``; ``subtune`` (0-based) is the one its replay plays."""
        song = DefmonSong.from_bytes(data)
        song.subtune = subtune
        return song

    def recognize(self, image: SidImage) -> "int | None":
        addr = _sid_format.find_signature(image.mem)
        return addr if addr >= 0 else None
