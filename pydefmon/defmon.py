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
``$5F00..$7166``              4711  256 sidTAB rows (15 bytes each) + sidcall metadata
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
:attr:`sidtab_region`, :attr:`arranger_v1` / ``v2`` / ``v3``,
:attr:`sidtab_dl`, :attr:`sidtab_jp`).
"""

from __future__ import annotations

from typing import Any

from pysidtracker import BaseSidParser, SidError, SidImage

from pydefmon._load_format import (
    CodecError,
    LOAD_ADDR,
    decode_load_stream,
    encode_ram_block,
)
from pydefmon._sid_format import depack_replay, find_signature
from pydefmon._goto80_format import decode_replay as decode_goto80_replay

LOAD_ADDRESS = LOAD_ADDR
STANDARD_SNAPSHOT_END = 0x7167
STANDARD_SNAPSHOT_SIZE = STANDARD_SNAPSHOT_END - LOAD_ADDRESS  # 22887


# ----------------------------------------------------------------------
# Note → SID-frequency lookup tables (lo/hi byte each, 128 entries
# indexed by the raw pattern note byte). The player's pitch
# oscillator reads these directly, so ``PatternEvent.sid_freq_word()``
# computes from the same tables and yields the exact 16-bit value the
# real defMON would latch into ``$D400/$D401``.
# ----------------------------------------------------------------------

_NOTE_PITCH_LO_HEX = (
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
)
NOTE_PITCH_LO = bytes.fromhex(_NOTE_PITCH_LO_HEX)
NOTE_PITCH_HI = bytes.fromhex(_NOTE_PITCH_HI_HEX)
assert len(NOTE_PITCH_LO) == 128
assert len(NOTE_PITCH_HI) == 128


class DefmonError(SidError, ValueError):
    """Raised when a file does not look like a defMON tune."""


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

    # ---- construction ----

    @classmethod
    def from_bytes(cls, raw: bytes) -> "DefmonSong":
        """Parse a defMON tune from raw container bytes.

        Handles both containers defMON tunes ship in:

        * a ``.prg`` editor workfile (``$1800`` load address + ``$D6C9``
          RLE body) -- the format defMON's editor saves; and
        * a PSID/RSID ``.sid`` *replay* file (the packed, relocatable
          player + compacted tune data HVSC ships), dispatched to
          :meth:`from_sid_bytes`.
        """
        if raw[:4] in (b"PSID", b"RSID"):
            return cls.from_sid_bytes(raw)
        if len(raw) < 4:
            raise DefmonError("file too short to contain a load address + body")
        load_addr = raw[0] | (raw[1] << 8)
        if load_addr != LOAD_ADDRESS:
            raise DefmonError(
                f"unexpected load address ${load_addr:04X}, "
                f"expected ${LOAD_ADDRESS:04X}"
            )
        body = raw[2:]
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
            raise DefmonError(f"LOAD-decode failed: {e}") from e
        song = cls()
        for addr, val in writes.items():
            if LOAD_ADDRESS <= addr < LOAD_ADDRESS + STANDARD_SNAPSHOT_SIZE:
                song.snapshot[addr - LOAD_ADDRESS] = val
        return song

    @classmethod
    def from_file(cls, path: str) -> "DefmonSong":
        with open(path, "rb") as f:
            return cls.from_bytes(f.read())

    @classmethod
    def from_sid_bytes(cls, raw: bytes) -> "DefmonSong":
        """Parse a defMON tune from a PSID/RSID ``.sid`` replay file.

        HVSC ships defMON tunes as PSID/RSID replay images (a relocatable
        player plus the tune's compacted data), a different container from
        the ``.prg`` editor workfile :meth:`from_bytes` decodes. The replay
        embeds the runtime data tables directly (no ``$D6C9`` RLE stream);
        :func:`pydefmon._sid_format.depack_replay` recognises defMON's
        player signature and re-expands the compacted pattern bodies and
        sidTAB rows into the editor-layout runtime image this class models.

        Raises :class:`DefmonError` if ``raw`` is not a recognised defMON
        replay (no player signature) or uses a packer variant whose compact
        data layout cannot be mapped to the editor layout.
        """
        image = SidImage.from_bytes(raw)
        snapshot = depack_replay(image)
        if snapshot is None:
            # Fall back to the older Goto80 compact-runtime encoding
            # (variable-length pattern streams + relocated sidTAB bodies)
            # that the standard editor-layout reconstruction can't map.
            snapshot = decode_goto80_replay(image)
        if snapshot is None:
            if find_signature(image.mem) < 0:
                raise DefmonError("not a defMON replay (player signature not found)")
            raise DefmonError(
                "unrecognised defMON replay data layout "
                "(compact/indirect packer variant not supported)"
            )
        return cls(snapshot)

    # ---- serialization ----

    def to_bytes(self) -> bytes:
        """Serialize a complete PRG (load address + body) via the
        $D6C9 LOAD-time encoder. When LOADed, writes ``self.snapshot``
        into RAM at ``$1800..$7166``. Zero ``$1800,X`` on active-linear
        rows and zero ``$1A00..$1AFF`` before encoding; ``$CF42`` and
        ``$D004`` rewrite both regions at LOAD time.
        """
        snap = bytearray(self.snapshot)
        OFF_1800 = 0x1800 - LOAD_ADDRESS
        OFF_1900 = 0x1900 - LOAD_ADDRESS
        OFF_1A00 = 0x1A00 - LOAD_ADDRESS
        for x in range(256):
            if snap[OFF_1900 + x] != 0:
                snap[OFF_1800 + x] = 0
        for x in range(256):
            snap[OFF_1A00 + x] = 0
        try:
            return encode_ram_block(bytes(snap), base_addr=LOAD_ADDRESS)
        except CodecError as e:
            raise DefmonError(f"LOAD-encode failed: {e}") from e

    def to_file(self, path: str) -> None:
        with open(path, "wb") as f:
            f.write(self.to_bytes())

    # ---- convenience properties ----

    @property
    def load_address(self) -> int:
        return LOAD_ADDRESS

    @property
    def end_address(self) -> int:
        """Address one past the last snapshot byte at runtime."""
        return LOAD_ADDRESS + len(self.snapshot)

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
        active and computes the runtime pointers post-LOAD via $CF42.
        ``unpacked_snapshot()`` reconstructs the live values."""
        return self._region(0x1800, 0x200)

    @property
    def pattern_pointer_table(self) -> memoryview:
        """``$1A00..$1AFF`` (256 bytes). Always zero in saved files;
        runtime fill done by defMON's $D004 secondary pass.
        ``unpacked_snapshot()`` computes ``$1A00,X``/``$1A80,X`` =
        low/high of ``$1F00 + X*$80``."""
        return self._region(0x1A00, 0x100)

    @property
    def voice_pattern_refs(self) -> memoryview:
        """``$1B00..$1DFF`` (768 bytes) + ``$1E00..$1EFF`` (256 bytes,
        DL). Spans two unrelated regions glued together for legacy
        convenience; prefer :attr:`arranger_v1`, :attr:`arranger_v2`,
        :attr:`arranger_v3`, and :attr:`sidtab_dl` (or
        :attr:`sidtab_jp`) for new code.

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
          (runtime pointer-hi) at LOAD time; the paired ``$1800,Y`` is
          rewritten to ``Y * $0F`` (pointer-lo).
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
        """``$5F00..$7166``. Sidtab rows + sidcall metadata. Each row
        is 15 bytes (``$0F`` stride); see ``sidtab_row()`` for
        decoding. Sidcall ``N`` starts at row ``N`` and runs through
        subsequent rows until a freeze or loop terminator -- see
        ``sidcall_frames()``.
        """
        return self._region(0x5F00, 0x7167 - 0x5F00)

    # ---- builders for from-scratch tune authoring ----

    def clear_arranger(self) -> None:
        """Zero the per-step pattern-reference arrays at
        ``$1B00..$1DFF`` (the V0/V1/V2 arranger arrays).

        Use this when you want to reset the arrangement without
        also wiping the sidTAB DL byte region — see :meth:`clear_dl`
        for that. :meth:`clear_song_table` calls both, in the right
        order, and is provided for callers that want the legacy
        all-in-one wipe.
        """
        for offset in (0x1B00, 0x1C00, 0x1D00):
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
        arranger arrays at ``$1B00..$1DFF`` AND the sidTAB DL byte
        region at ``$1E00..$1EFF``.

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

    def set_step(self, y: int, *, v1: int = 0, v2: int = 0, v3: int = 0) -> None:
        """Set song step ``y`` to play patterns ``v1`` / ``v2`` / ``v3``
        on the three voices (use 0 for a silent voice). Pattern
        indices must be 0-127. To create a jump entry, call
        ``set_jump`` instead.
        """
        if not 0 <= y <= 255:
            raise ValueError(f"song step must be 0..255, got {y}")
        for name, val in (("v1", v1), ("v2", v2), ("v3", v3)):
            if not 0 <= val <= 0x7F:
                raise ValueError(f"{name} pattern index must be 0..127, got {val}")
        OFF_1B = 0x1B00 - LOAD_ADDRESS
        OFF_1C = 0x1C00 - LOAD_ADDRESS
        OFF_1D = 0x1D00 - LOAD_ADDRESS
        self.snapshot[OFF_1B + y] = v1
        self.snapshot[OFF_1C + y] = v2
        self.snapshot[OFF_1D + y] = v3

    def set_jump(self, y: int, *, target: int, count: int = 0) -> None:
        """Mark song step ``y`` as a jump command. The song will jump
        to step ``target`` ``count`` times before falling through.
        ``count == 0`` is unconditional (infinite loop).

        On disk: ``$1B00,y = $FF`` (the arranger's jump marker
        recognised by ``_arranger_advance`` in ``defmon_player``),
        with ``$1C00,y = target`` and ``$1D00,y = count``.
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

        Returns a list of ``SidcallFrame`` covering the rows the
        player would visit, from ``start_y`` until either a freeze
        frame (control byte ``$80+``), a loop back to a previously-
        visited row, or ``max_frames`` (safety bound).
        """
        unpacked = self.unpacked_snapshot()
        sidtab = bytes(self.sidtab_region)
        OFF_1800 = 0x0000
        OFF_1900 = 0x0100
        OFF_1E00 = 0x0600

        frames: list[SidcallFrame] = []
        seen: set[int] = set()
        y = start_y & 0xFF
        loopback_guard = 0
        while len(frames) < max_frames:
            if y >= 256:
                break
            if unpacked[OFF_1900 + y] == 0:
                if loopback_guard >= 4:
                    break
                loopback_guard += 1
                y = unpacked[OFF_1800 + y]
                continue
            if y in seen:
                frames[-1].loops_to = y
                break
            seen.add(y)
            loopback_guard = 0
            row_bytes = sidtab[y * 15 : y * 15 + 15]
            if len(row_bytes) < 15:
                break
            control = unpacked[OFF_1E00 + y]
            # Pass the raw on-disk JP marker (from self.snapshot, not the
            # unpacked form -- unpacked_snapshot rewrites $11 -> runtime
            # pointer-hi). dl == control by definition.
            jp_marker = self.snapshot[0x1900 - LOAD_ADDRESS + y]
            row = SidtabRow.parse(y, bytes(row_bytes), jp=jp_marker, dl=control)
            frames.append(SidcallFrame(y, row, control))
            if control & 0x80:
                break
            y += 1
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
          Pattern 0 is reserved as the "silent voice on this step"
          target and is never used as a remap destination; arranger
          rows whose V0 entry is ``$FF`` (jump command) are skipped
          entirely because their V1/V2 bytes are target/count rather
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
        OFF_1B = 0x1B00 - LOAD_ADDRESS
        OFF_1C = 0x1C00 - LOAD_ADDRESS
        OFF_1D = 0x1D00 - LOAD_ADDRESS
        for idx in range(256):
            if self.snapshot[OFF_1B + idx] == 0xFF:
                # jump-command row: V1=target, V2=count, neither a
                # pattern index.
                continue
            for arr_base in (OFF_1B, OFF_1C, OFF_1D):
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

    def unpacked_snapshot(self) -> bytearray:
        """Return a copy of the snapshot with runtime pointers
        reconstructed: ``$1800,X``/``$1900,X`` filled with the active
        per-step pointer ``$5F00 + X*$0F`` (where defMON's saver left
        ``$11`` markers), and ``$1A00,X``/``$1A80,X`` filled with the
        low/high of ``$1F00 + X*$80``. Models the post-LOAD state
        after defMON's $CF42 + $D004 secondary passes have run."""
        out = bytearray(self.snapshot)
        OFF_1800 = 0x1800 - LOAD_ADDRESS
        OFF_1900 = 0x1900 - LOAD_ADDRESS
        addr = 0x5F00
        for x in range(256):
            if out[OFF_1900 + x] != 0:
                out[OFF_1800 + x] = addr & 0xFF
                out[OFF_1900 + x] = (addr >> 8) & 0xFF
            addr += 0x0F
        OFF_1A00 = 0x1A00 - LOAD_ADDRESS
        OFF_1A80 = 0x1A80 - LOAD_ADDRESS
        for x in range(128):
            ptr = 0x1F00 + x * 0x80
            out[OFF_1A00 + x] = ptr & 0xFF
            out[OFF_1A80 + x] = (ptr >> 8) & 0xFF
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
    WGh    $D404         Voice control byte (waveform / gate / sync / ring).
                          Becomes ``ctrl_main`` on the voice.
    WGl    (XOR mask)    XOR'd into ``ctrl_main`` each frame before emit.
                          Lets tunes flicker waveform/gate mid-note.
    AD     $D405         Attack (high nibble) / decay (low nibble).
    SR     $D406         Sustain (high nibble) / release (low nibble).
    TR     (note byte)   New ``current_note`` for the voice. Bit 7 set =
                          absolute note value; bit 7 clear = relative,
                          added to the voice's transpose buffer (= the
                          note byte from the most recent GATE_N).
    AF     (slide mode)  Pitch-slide / portamento mode for the voice:
                          ``$00`` = no slide, ``$01..$7F`` = portamento
                          toward ``current_note + AF``, ``$80..$FF`` =
                          active slide at a rate from the per-mode LUT.
    PW     $D402/$D403   Pulse width — sets ``pulse_hi`` to the byte
                          verbatim and ``pulse_lo`` to ``byte & $F0``
                          (12-bit ``$YX`` PW encoding).
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

    _LOW_BITS = {7: "WGl", 6: "WGh", 5: "AD", 4: "SR", 3: "TR", 2: "AF", 1: "PW"}
    _LOW_ORDER = (6, 7, 5, 4, 3, 2, 1)  # WGh, WGl, AD, SR, TR, AF, PW
    _HIGH_BITS = {7: "PS", 6: "RE", 5: "FV", 4: "CP", 3: "ACID"}
    _HIGH_ORDER = (7, 6, 5, 4, 3)
    _HIGH_WIDTH = {"PS": 1, "RE": 1, "FV": 1, "CP": 1, "ACID": 2}

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
        row.low_bitmap = raw[0]
        pos = 1
        for bit in cls._LOW_ORDER:
            if row.low_bitmap & (1 << bit):
                if pos >= 15:
                    break
                setattr(row, cls._LOW_BITS[bit], raw[pos])
                pos += 1
        if pos < 15:
            row.high_bitmap = raw[pos]
            pos += 1
            for bit in cls._HIGH_ORDER:
                if row.high_bitmap & (1 << bit):
                    name = cls._HIGH_BITS[bit]
                    w = cls._HIGH_WIDTH[name]
                    if pos + w > 15:
                        break
                    if w == 2:
                        v = (raw[pos] << 8) | raw[pos + 1]
                    else:
                        v = raw[pos]
                    setattr(row, name, v)
                    pos += w
        row.trailing = bytes(raw[pos:])
        row.jp = jp
        row.dl = dl
        return row

    def to_bytes(self) -> bytes:
        """Inverse of :meth:`parse`: pack this row's columns + trailing
        bytes back into the 15-byte on-disk form."""
        out = bytearray()
        low_bm = 0
        for bit in self._LOW_ORDER:
            if getattr(self, self._LOW_BITS[bit]) is not None:
                low_bm |= 1 << bit
        out.append(low_bm)
        for bit in self._LOW_ORDER:
            v = getattr(self, self._LOW_BITS[bit])
            if v is not None:
                out.append(v & 0xFF)
        high_bm = 0
        for bit in self._HIGH_ORDER:
            if getattr(self, self._HIGH_BITS[bit]) is not None:
                high_bm |= 1 << bit
        out.append(high_bm)
        for bit in self._HIGH_ORDER:
            name = self._HIGH_BITS[bit]
            v = getattr(self, name)
            if v is None:
                continue
            if self._HIGH_WIDTH[name] == 2:
                out.append((v >> 8) & 0xFF)
                out.append(v & 0xFF)
            else:
                out.append(v & 0xFF)
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
        valid = set(cls._LOW_BITS.values()) | set(cls._HIGH_BITS.values())
        for name, v in values.items():
            if name not in valid:
                raise ValueError(f"unknown sidtab column: {name!r}")
            setattr(row, name, int(v))
        row.trailing = bytes(trailing)
        return row.to_bytes()

    def values(self) -> dict:
        """Return ``{column_name: value}`` for columns with overrides set."""
        out = {}
        for name in (
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
        ):
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

    * ``FLAG_ALT`` (``$80``) — pattern-end / song-advance trigger.
      Setting this on the last event of a pattern hands control to
      the arranger to walk to the next song step. The flag's low
      nibble (``duration``) becomes the timer reload for the NEXT
      pattern step's gap.
    * ``FLAG_GATE_A`` (``$40``) — apply ``slot_a`` to the voice's
      sidcall layer 1: sets ``sc1_row_idx = slot_a`` and counter
      to 0 so the cascade fetches at row ``slot_a`` next frame.
    * ``FLAG_GATE_B`` (``$20``) — same for sidcall layer 2 via
      ``slot_b``.
    * ``FLAG_GATE_N`` (``$10``) — apply ``note`` as the voice's new
      ``current_note`` AND store the same byte in the voice's
      transpose buffer (the latter is the base for subsequent
      sidTAB TR-relative shifts). Also zeros the voice's
      slide-accumulator + slide-mode so the new note starts clean.
    * bits 3..0 — ``duration``: how many frames the player holds
      this row before reading the next pattern step. The player's
      cadence is 1 main-tick NMI per ``cycles_per_frame`` cycles
      (~24 ms for .GLOW WORM), so ``duration=4`` means ~96 ms hold
      under PAL.

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
        if not 1 <= note <= 0x7F:
            raise ValueError(f"note byte must be 1..127, got ${note:02X}")
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
        """Build a pure-delay event: no gates fired, ``duration``-frame
        wait before the next event."""
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

    # Historical 120-entry, ``note - 1``-indexed pitch table. Retained
    # only as a backwards-compat alias of the LSB/MSB byte-tables now
    # exported by :mod:`pydefmon.defmon`; **do not consult this for
    # new code**, use ``NOTE_PITCH_LO[note] | (NOTE_PITCH_HI[note] << 8)``
    # instead — the player walks the byte tables, and the two were
    # off by one semitone here until this comment was written.
    _NOTE_FREQ_WORDS = (
        0x0046,
        0x004A,
        0x004E,
        0x0053,
        0x0058,
        0x005D,
        0x0062,
        0x0068,
        0x006E,
        0x0075,
        0x007C,
        0x0083,
        0x008B,
        0x0093,
        0x009C,
        0x00A6,
        0x00AF,
        0x00BA,
        0x00C5,
        0x00D1,
        0x00DD,
        0x00EA,
        0x00F8,
        0x0107,
        0x0116,
        0x0127,
        0x0139,
        0x014B,
        0x015F,
        0x0174,
        0x018A,
        0x01A1,
        0x01BA,
        0x01D4,
        0x01F0,
        0x020E,
        0x022D,
        0x024E,
        0x0271,
        0x0296,
        0x02BE,
        0x02E7,
        0x0314,
        0x0342,
        0x0374,
        0x03A9,
        0x03E0,
        0x041B,
        0x045A,
        0x049C,
        0x04E2,
        0x052D,
        0x057B,
        0x05CF,
        0x0627,
        0x0685,
        0x06E8,
        0x0751,
        0x07C1,
        0x0837,
        0x08B4,
        0x0938,
        0x09C4,
        0x0A59,
        0x0AF7,
        0x0B9D,
        0x0C4E,
        0x0D0A,
        0x0DD0,
        0x0EA2,
        0x0F81,
        0x106D,
        0x1167,
        0x1270,
        0x1389,
        0x14B2,
        0x15ED,
        0x173B,
        0x189C,
        0x1A13,
        0x1BA0,
        0x1D45,
        0x1F02,
        0x20DA,
        0x22CE,
        0x24E0,
        0x2711,
        0x2964,
        0x2BDA,
        0x2E76,
        0x3139,
        0x3426,
        0x3740,
        0x3A89,
        0x3E04,
        0x41B4,
        0x459C,
        0x49C0,
        0x4E23,
        0x52C8,
        0x57B4,
        0x5CEB,
        0x6272,
        0x684C,
        0x6E80,
        0x7512,
        0x7C08,
        0x8368,
        0x8B39,
        0x9380,
        0x9C45,
        0xA590,
        0xAF68,
        0xB9D6,
        0xC4E3,
        0xD099,
        0xDD00,
        0xEA24,
        0xF810,
        0xFFFF,
    )

    SID_PAL_CLOCK_HZ = 985248

    def sid_freq_word(self) -> "int | None":
        """Return the 16-bit SID freq word the player will latch for
        this event's note byte, or ``None`` for non-pitch values.

        Uses the same ``NOTE_PITCH_LO/HI`` tables the player walks
        (indexed by ``note`` directly — *not* ``note - 1``). The
        per-voice ``pitch_base`` detune (V0=+0, V1=+1, V2=+2 on
        freq_lo) is *not* applied here; callers that need a
        per-voice exact match should add it themselves.
        """
        if self.note == 0 or self.note & 0x80:
            return None
        return (NOTE_PITCH_HI[self.note] << 8) | NOTE_PITCH_LO[self.note]

    def frequency_hz(self) -> "float | None":
        word = self.sid_freq_word()
        if word is None:
            return None
        return word * self.SID_PAL_CLOCK_HZ / (1 << 24)

    def note_name(self) -> str:
        if self.note == 0:
            return "---"
        if self.note & 0x80:
            return f"${self.note:02X}"
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

    error_class: type = DefmonError

    def parse(self, data: bytes, **kwargs: Any) -> DefmonSong:
        return DefmonSong.from_bytes(data)

    def recognize(self, image: SidImage) -> "int | None":
        addr = find_signature(image.mem)
        return addr if addr >= 0 else None
