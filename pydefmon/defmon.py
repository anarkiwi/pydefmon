"""defMON tune snapshot reader/writer.

A defMON tune is a PRG that loads at ``$1800`` and contains a
``$D6C9`` LOAD-decoder body (see ``pydefmon._load_format``).
``DefmonSong`` exposes the decoded RAM image covering
``$1800..$7166`` (22887 bytes; the runtime image defMON's editor
sees post-LOAD plus its secondary $CF42 / $D004 passes).

Quick start::

    from pydefmon.defmon import DefmonSong, PatternEvent

    song = DefmonSong.from_file("tune.prg")
    print(song.pattern(0).hex())              # 128-byte pattern body
    print(song.pattern_events(0))             # decoded events
    print(song.sidtab_row(0))                 # 15-byte sidtab row decoded
    song.to_file("out.prg")

Snapshot layout (the 22887-byte buffer ``song.snapshot`` covers
``$1800..$7166``; region accessors return ``memoryview`` slices into
it):

  ``$1800..$19FF``  song-position arrays ($11 markers on disk;
                    ``unpacked_snapshot()`` reconstructs the runtime
                    pointer-cache form)
  ``$1A00..$1AFF``  pattern pointer table (zero on disk; ``$D004``
                    fills at runtime)
  ``$1B00..$1EFF``  voice_pattern_refs -- per-voice pattern indices
                    keyed by song step
  ``$1F00..$5EFF``  128 pattern bodies of 128 bytes (32 events x 4 bytes)
  ``$5F00..$7166``  sidtab + sidcall metadata; visible sidtab rows are
                    15-byte packed-bitmap records (see ``SidtabRow``)
"""

from __future__ import annotations

from pydefmon._load_format import (
    CodecError,
    LOAD_ADDR,
    decode_load_stream,
    encode_ram_block,
)

LOAD_ADDRESS = LOAD_ADDR
STANDARD_SNAPSHOT_END = 0x7167
STANDARD_SNAPSHOT_SIZE = STANDARD_SNAPSHOT_END - LOAD_ADDRESS  # 22887


class DefmonError(ValueError):
    """Raised when a file does not look like a defMON tune."""


class DefmonSong:
    """A defMON tune.

    ``song.snapshot`` is the 22887-byte mutable decoded memory image.
    Region accessors (``pattern(n)``, ``voice_pattern_refs``, etc.)
    return ``memoryview`` slices of ``snapshot``, so writes through
    them mutate the underlying buffer.
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
        """Parse a complete PRG file (load address + body)."""
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

    def clear_song_table(self) -> None:
        """Wipe ``voice_pattern_refs`` to all zero -- every song step
        plays "no pattern" on every voice. Useful before building a
        song from scratch with ``set_step`` / ``set_jump``."""
        for i in range(0x400):
            self.voice_pattern_refs[i] = 0

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
        ``count == 0`` is unconditional (infinite loop)."""
        if not 0 <= y <= 255:
            raise ValueError(f"song step must be 0..255, got {y}")
        if not 0 <= target <= 255:
            raise ValueError(f"jump target must be 0..255, got {target}")
        if not 0 <= count <= 255:
            raise ValueError(f"loop count must be 0..255, got {count}")
        OFF_1B = 0x1B00 - LOAD_ADDRESS
        OFF_1C = 0x1C00 - LOAD_ADDRESS
        OFF_1D = 0x1D00 - LOAD_ADDRESS
        self.snapshot[OFF_1B + y] = 0x80
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
    """Decoded view of one 15-byte sidtab row, plus two co-addressed
    companion bytes (``jp`` and ``dl``) the cascade reads alongside the
    row.

    On-disk row body (15 bytes at ``$5F00 + index * $0F``)::

      byte 0           low-half override bitmap
      bytes 1..N       low values, packed in screen-column order
      byte 1+N         high-half override bitmap
      bytes 2+N..M     high values, packed in screen-column order
                       (ACID is 2 bytes; others 1 byte each)
      bytes M+1..14    zero pad

    A column attribute is ``None`` when its bit is not set in the
    bitmap (= "no override on this row, use inherited default").

    Companion bytes (per-row, addressed by the same ``index``; live in
    separate RAM regions and are NOT part of the 15-byte pack):

    * ``jp`` -- ``$1900,index`` JP marker. On disk: ``$11`` = active
      linear (cascade advances to ``index + 1`` after DL underflow);
      ``$00`` = inactive or JUMP-SOURCE (paired ``$1800,index`` holds
      target row index). Read by :meth:`DefmonSong.sidtab_row` and
      :meth:`DefmonSong.sidcall_frames`.
    * ``dl`` -- ``$1E00,index`` DL byte. ``$00..$7F`` = hold this row
      for ``dl + 1`` frames; ``$80..$FF`` = STop (freeze + gate-off).

    Both companions are ``None`` when the caller didn't supply them
    (e.g. ``SidtabRow.parse(index, raw)`` called directly without going
    through :meth:`DefmonSong.sidtab_row`). They round-trip through
    :meth:`__repr__`, :meth:`__eq__`, and :meth:`__hash__` so callers
    can compare rows including their cascade-side context.

    Column -> SID register mapping:

    ====  =================================================
    WGh   ``$D404`` voice ctrl (waveform / gate / sync / ring)
    WGl   ctrl XOR mask applied each frame
    AD    ``$D405`` attack(hi) / decay(lo)
    SR    ``$D406`` sustain(hi) / release(lo)
    TR    transpose / note value
    AF    arpeggio / portamento (combined)
    PW    ``$D402/$D403`` pulse width
    PS    bouncing pulse-width sweep
    RE    ``$D417`` resonance + voice-select
    FV    ``$D418`` master volume + filter mode
    CP    ``$D415/$D416`` filter cutoff
    ACID  parametric cutoff sweep (1 or 2 bytes)
    ====  =================================================
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
    """One 4-byte event from a pattern body. Fields are mutable so
    callers can edit events in place and write them back via
    ``DefmonSong.set_pattern_events``.

    Layout::

      byte 0  flag -- see bit semantics below
      byte 1  slot_a -- sidcall starting-row index (layer 0)
      byte 2  slot_b -- sidcall starting-row index (layer 1)
      byte 3  note   -- note byte 1..120 (or 0 for "no note")

    Flag bits:

    * bit 7 (``FLAG_ALT``) -- pattern-end / song-advance trigger.
    * bit 6 (``FLAG_GATE_A``) -- apply ``slot_a`` (re-arm sidcall layer 0).
    * bit 5 (``FLAG_GATE_B``) -- apply ``slot_b`` (re-arm sidcall layer 1).
    * bit 4 (``FLAG_GATE_N``) -- apply ``note`` (start playing this note).
    * bits 3-0 -- duration nibble (0-15): synchronised pause before the
      next event is fetched.
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
        if self.note == 0 or self.note & 0x80:
            return None
        idx = self.note - 1
        if idx >= len(self._NOTE_FREQ_WORDS):
            return None
        return self._NOTE_FREQ_WORDS[idx]

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
