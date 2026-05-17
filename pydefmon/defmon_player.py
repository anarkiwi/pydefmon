"""Python defMON player IRQ -> SID register stream.

Per-frame model of defMON's player IRQ ($1003 entry, body at $1022).
Consumes a :class:`DefmonSong` snapshot (post-LOAD form) and emits
the SID register writes that defMON's $1022 body would emit on a
real C64, in per-frame order.

Scope:

  - 3 voice slots (SID#1 V0/V1/V2). SID#2 + stereo out of scope.
  - Per-voice pattern advance over the 4-byte step layout
    ``(flag, slot_a, slot_b, note)``; flag bits ``ALT=$80``,
    ``GATE_A=$40``, ``GATE_B=$20``, ``GATE_N=$10``, low nibble =
    duration.
  - sidcall step counter sourced from the DL column at ``$1E00,Y``
    (``DefmonSong.sidtab_dl``). ``$00..$7F`` hold the row for
    ``N + 1`` frames; ``$80..$FF`` STops the slot until a new
    ``GATE_A`` / ``GATE_B`` re-arms it.
  - Arranger byte ``$FF`` is a ``FF XX YY`` jump command (XX =
    target row, YY = repeat count; ``YY = 0`` is infinite loop).
    Per-voice repeat counter walks the count down, then falls
    through to ``$FF + 3``.
  - sidTAB row apply mirrors ``$16B0``: read the low bitmap, then
    dispatch only the bits set to the correct per-voice working
    slot. WGl (bit 7) is captured as an EOR mask applied to the
    control byte every frame; PW (bit 1) writes the row byte to
    both pulse-hi (full byte) and pulse-lo (byte & $F0) per the
    12-bit ``$YX`` encoding.

Not modelled:

  - $14EE startup RNG ($10CE / $10D4 patches): hardcoded to the
    entropy-bit-clear branch ($10CE = $02, $10D4 = NOP).
  - AF column (slide direction stored on the voice but not applied).

Per-frame SID write order: ``PWlo PWhi Flo Fhi SR AD CTRL`` per
voice (V0/V1/V2), then ``$D417 RES/route``, ``$D418 volume``,
``$D416 filter cutoff hi``.

CLI: ``python3 -m pydefmon_player INPUT.prg OUTPUT.wav
[--seconds 60] [--model MOS8580|MOS6581]``.
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from pydefmon.defmon import DefmonSong, LOAD_ADDRESS

SID_REG_BASE = 0xD400
# defMON installs a CIA-1 Timer A interrupt at a tune-specific rate
# (NOT the 50 Hz PAL raster IRQ). 23546 cyc/IRQ (~41.84 Hz on PAL)
# is the .GLOW WORM rate; per-tune rates are read at runtime from
# $715A/$715B and used to override this default.
PAL_CYCLES_PER_FRAME = 23546

PATTERN_BANK_BASE = 0x1F00
PATTERN_STRIDE = 0x80
STEPS_PER_PATTERN = 32

ARRANGER_BASE = {0: 0x1B00, 1: 0x1C00, 2: 0x1D00}

SIDTAB_DATA_BASE = 0x5F00
SIDTAB_ROW_STRIDE = 15

# Per-voice X offset into the $1019/$104A/$107B working-record bands.
# Used by $16B0 to index the patch slots; we mirror it on the Python
# Voice records by holding state per-voice without indirection.
VOICE_X = {0: 0x00, 1: 0x31, 2: 0x62}

# Flag-byte bit constants on the 4-byte pattern step.
FLAG_ALT = 0x80
FLAG_GATE_A = 0x40
FLAG_GATE_B = 0x20
FLAG_GATE_N = 0x10
FLAG_DUR_MASK = 0x0F

# Low-bitmap bit -> column dispatch (matches $16B0 body +
# SidtabRow._LOW_BITS). Each entry: (bit, attribute_name, width_bytes).
# Listed in the order $16B0 walks the bitmap (bit 6, bit 7, bit 5, ...).
LOW_BITMAP_ORDER = (
    (6, "WGh"),  # ctrl_main -> $103B,x
    (7, "WGl"),  # ctrl_eor mask -> $103D,x (applied per frame in band)
    (5, "AD"),  # attack/decay -> $1039,x
    (4, "SR"),  # sustain/release -> $1037,x
    (3, "TR"),  # note (with optional transpose from $12ED,x) -> $137F,x
    (2, "AF"),  # slide mode -> $101B,x (recorded, not yet applied)
    (1, "PW"),  # pulse-width -> $1025,x (full) + $1023,x (& $F0)
)

# High-bitmap dispatch -- second bitmap byte. Width 1 unless noted.
HIGH_BITMAP_ORDER = (
    (7, "PS", 1),  # pulse sweep (not modelled this pass)
    (6, "RE", 1),  # resonance + routing -> $10AA / $D417
    (5, "FV", 1),  # filter mode + volume -> $D418
    (4, "CP", 1),  # cutoff delta (treated as absolute this pass)
    (3, "ACID", 2),  # 16-bit cutoff slide command
)


# ----------------------------------------------------------------------
# Pitch LUTs $159C (lo) / $1638 (hi) -- 128 bytes each, extracted from
# defmon-src/artefacts/defmon-static.bin. These are what defMON's player
# IRQ reads via Y = note byte; we embed them so the player doesn't need
# the static binary at runtime.
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


# ----------------------------------------------------------------------
# Pitch-slide oscillator LUTs ($14F8..$1737, 576 bytes contiguous) --
# extracted from defmon-static.bin. The pitch-slide oscillator at $1405
# indexes them by ``Y = slide_mode << 1`` (slide rate) or ``Y = current
# note`` (target pitch). Multiple labelled LUTs alias the same backing
# bytes (the pitch-LUT and slide-rate-LUT share byte storage at
# different offsets); we keep the full 576-byte block and address it by
# base+offset.
# ----------------------------------------------------------------------

_LUT_BASE_ADDR = 0x14F8
_LUT_BLOCK_HEX = (
    "ff8d12d48d0ed48d0fd4a9208d12d4ad1bd44a9009a9008dce10a90ad007a902"
    "8dce10a9ea8dd41028608deb10a017a9009900d48810fa8db6108dbe108db910"
    "8dc0108dca108daa108daf1020ee14a262a9009def129d82139d39109d37109d"
    "3b109d3d109d1b109d1e109d7f13a9ff9de0129d7313cb3110d7a9808dd91060"
    "000001010202040408080c0c111214151617191a1c1d1f21232527292c2e3134"
    "373b3e42464a4e53585d62686e757c838b939ca6afbac5d1ddeaf8071627394b"
    "5f748aa1bad4f00e2d4e7196bee7144274a9e01b5a9ce22d7bcf2785e851c137"
    "b438c459f79d4e0ad0a2816d677089b2ed3b9c13a04502dacee01164da763926"
    "408904b49cc023c8b4eb724c801208683980459068d6e399002410ff00000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000010101010101010101"
    "010101020202020202020303030303040404040505050606060707080809090a"
    "0a0b0c0d0d0e0f10111213141517181a1b1d1f20222427292b2e3134373a3e41"
    "45494e52575c62686e757c838b939ca5afb9c4d0ddeaf8ff85fba000b1fbf05a"
    "0a85961006c8b1fb9d3b109006c8b1fb9d3d1024965006c8b1fb9d3910a59629"
    "20f006c8b1fb9d3710a5962910f00ec8b1fb3004187ded12297f9d7f13a59629"
    "08f006c8b1fb9d1b10a5962904f00bc8b1fb9d251029f09d2310c8b1fbf07e0a"
    "85969006c8b1fb9d1e10a596f06f2980f024c8b1fbf0092908d00eb1fb4c4b17"
)
_LUT_BLOCK = bytes.fromhex(_LUT_BLOCK_HEX)
assert len(_LUT_BLOCK) == 576


def _lut(base: int, y: int) -> int:
    """Read byte at address (base + y) from the embedded LUT block.
    ``base`` must be in $14F8..$1737; ``y`` is 0..255."""
    addr = (base + y) & 0xFFFF
    off = addr - _LUT_BASE_ADDR
    if 0 <= off < len(_LUT_BLOCK):
        return _LUT_BLOCK[off]
    return 0  # outside our embedded range; defMON in practice never indexes there


# Labelled LUT bases per the disassembly.
SLIDE_DEC_LO_BASE = 0x14F8
SLIDE_INC_LO_BASE = 0x1578
SLIDE_DEC_HI_BASE = 0x1594
SLIDE_INC_HI_BASE = 0x1614
NOTE_PITCH_LO_BASE = 0x159C  # mirrors NOTE_PITCH_LO[] (offset 0..127 by note byte)
NOTE_PITCH_HI_BASE = 0x1638  # mirrors NOTE_PITCH_HI[]


# ----------------------------------------------------------------------
# Voice runtime record. Mirrors the per-voice working state defMON's
# player keeps interleaved with the SID-write band ($1019/$104A/$107B);
# we keep it as Python state per voice rather than chasing the X-index
# scheme.
# ----------------------------------------------------------------------


@dataclass
class Voice:
    # SID-write patch slots (operands of LDX/LDA in the $1022 band).
    freq_lo: int = 0
    freq_hi: int = 0
    pulse_lo: int = 0
    pulse_hi: int = 0
    sr: int = 0
    ad: int = 0
    ctrl_main: int = 0  # $103B,x -- LDA-imm operand before EOR
    ctrl_eor: int = 0  # $103D,x -- EOR-imm operand applied each frame

    # Slide / portamento working state ($1019/$101A,x slide accumulator,
    # $101B,x slide mode, $101E,x PS depth, $101F,x pitch base). Driven
    # by the $1405 pitch-slide oscillator that runs after the cascade
    # but before SID-register emission.
    slide_accum_lo: int = 0  # $1019,x
    slide_accum_hi: int = 0  # $101A,x
    slide_mode: int = 0  # $101B,x -- 0=hold, $80..$FF active slide, $01..$7F portamento
    ps_depth: int = 0  # $101E,x -- pulse-width sweep depth + direction
    pitch_base: int = 0  # $101F,x -- unused offset (== 0 for .GLOW WORM)

    # Pattern advance state (per-voice).
    row_timer: int = 0  # frames remaining at the current step
    pattern_step_idx: int = 0
    transpose: int = 0
    current_note: int = 0  # $137F,x -- updated by GATE_N or by sidTAB TR
    pat_base_lo: int = 0  # $1186/$120E/$1296 -- per-voice pat_base lo
    pat_base_hi: int = 0  # $1187/$120F/$1297 -- per-voice pat_base hi

    # sidcall cascade per voice (2 layers). step_counter semantics
    # match defMON's $12DF cascade body: $00 = fetch next frame;
    # $01..$7F = hold (decrement per frame, fetch on underflow);
    # $80..$FF = skip (frozen / STop). Player init starts both layers
    # at $FF so they're silent until a pattern row's GATE_A / GATE_B
    # resets the counter to $00 (= "fetch now").
    sc1_row_idx: int = 0
    sc1_step_counter: int = 0xFF
    sc2_row_idx: int = 0
    sc2_step_counter: int = 0xFF


# ----------------------------------------------------------------------
# Sidtab row apply -- mirrors $16B0.
# ----------------------------------------------------------------------


def _apply_sidtab_row(voice: Voice, raw: bytes, *, transpose_buffer: int) -> int:
    """Apply a 15-byte sidTAB row to ``voice``'s working slots, in the
    order $16B0 walks the row. Returns the (possibly updated)
    ``transpose_buffer`` (= $12ED,x in defMON terms)."""
    if not raw or raw[0] == 0:
        return transpose_buffer

    low_bm = raw[0]
    pos = 1

    # bit 6 -> WGh -> ctrl_main
    if low_bm & (1 << 6):
        if pos >= len(raw):
            return transpose_buffer
        voice.ctrl_main = raw[pos]
        pos += 1
    # bit 7 -> WGl -> ctrl_eor (EOR mask applied every frame)
    if low_bm & (1 << 7):
        if pos >= len(raw):
            return transpose_buffer
        voice.ctrl_eor = raw[pos]
        pos += 1
    # bit 5 -> AD
    if low_bm & (1 << 5):
        if pos >= len(raw):
            return transpose_buffer
        voice.ad = raw[pos]
        pos += 1
    # bit 4 -> SR
    if low_bm & (1 << 4):
        if pos >= len(raw):
            return transpose_buffer
        voice.sr = raw[pos]
        pos += 1
    # bit 3 -> TR. Per $16B0: if byte has bit 7 set, store raw; else
    # store (byte + transpose_buffer) & $7F. Stores to current_note
    # ($137F,x) only -- freq registers are written by the $1405 pitch
    # slide oscillator, which runs after the cascade each frame.
    if low_bm & (1 << 3):
        if pos >= len(raw):
            return transpose_buffer
        tr_byte = raw[pos]
        if tr_byte & 0x80:
            note = tr_byte & 0x7F
        else:
            note = (tr_byte + transpose_buffer) & 0x7F
        voice.current_note = note
        pos += 1
    # bit 2 -> AF -> slide mode (recorded, not yet acted on)
    if low_bm & (1 << 2):
        if pos >= len(raw):
            return transpose_buffer
        voice.slide_mode = raw[pos]
        pos += 1
    # bit 1 -> PW. Single byte -> pulse_hi (full) + pulse_lo (byte & $F0).
    if low_bm & (1 << 1):
        if pos >= len(raw):
            return transpose_buffer
        b = raw[pos]
        voice.pulse_hi = b
        voice.pulse_lo = b & 0xF0
        pos += 1

    return transpose_buffer


# ----------------------------------------------------------------------
# Main player.
# ----------------------------------------------------------------------


class DefmonPlayer:
    """Per-frame model of defMON's player IRQ.

    Construct from a post-LOAD :class:`DefmonSong` (the song's
    ``unpacked_snapshot()`` is consumed so $1800/$1900 hold runtime
    pointer pairs and $1A00/$1A80 hold pat_base entries -- but we
    address everything by structured field, so the unpacked form is
    just convenience for direct-ram access if needed).
    """

    def __init__(self, song: DefmonSong) -> None:
        # We work directly off the post-LOAD form so $CFB8's bit-7
        # marker on step-31 flag bytes is in place.
        self.snapshot = song.unpacked_snapshot()
        self.song = song

        # Per-tune effective player-IRQ cycle interval. defMON installs
        # a CIA2 Timer A interrupt with the lo/hi bytes at $715A/$715B
        # ($0A78 LDA $715A; STA $DD04 / LDA $715B; STA $DD05). $715C
        # holds the sub-frame count: the NMI fires every $715A/$715B
        # cycles and on every $715C-th NMI it runs the full $1003 tick
        # (otherwise the sub-frame $1006 path). Effective full-tick
        # rate = ($715A | $715B<<8) * $715C cycles.
        cia_lo = self.snapshot[0x715A - LOAD_ADDRESS]
        cia_hi = self.snapshot[0x715B - LOAD_ADDRESS]
        sub_count = self.snapshot[0x715C - LOAD_ADDRESS] or 1
        self.cycles_per_frame = (cia_lo | (cia_hi << 8)) * sub_count
        if self.cycles_per_frame == 0:
            self.cycles_per_frame = PAL_CYCLES_PER_FRAME
        # Per-tune sub-frame count ($715C). Drives extra cascade
        # ticks per main play_frame, modelling defMON's $1006 sub-frame
        # update that JSRs $1022 (with $10D8 patched) and then JMPs to
        # $12DF (cascade tail).
        self.sub_frame_count = sub_count
        # Mirror $CFB8's idempotent OR onto step 31's flag byte. The
        # snapshot from unpacked_snapshot() doesn't necessarily have
        # bit 7 set on step 31 of every pattern (only patterns the
        # source author touched), so apply it explicitly.
        for i in range(128):
            off = (PATTERN_BANK_BASE + i * PATTERN_STRIDE + 0x7C) - LOAD_ADDRESS
            if 0 <= off < len(self.snapshot):
                self.snapshot[off] |= 0x80

        # Per-voice working records at $1019 (V0) / $104A (V1) / $107B
        # (V2) hold a hardcoded pitch_base value at offset +6
        # ($101F / $1050 / $1081 = $00 / $01 / $02 in the static
        # binary). The pitch-slide oscillator at $1453 adds this byte
        # to NOTE_PITCH_LO[note] before storing into the freq_lo patch
        # slot, producing a 0/+1/+2 detune across voices. These bytes
        # live below the prg LOAD_ADDRESS so they're not in the
        # snapshot; hardcode them.
        self.voices = [Voice(), Voice(), Voice()]
        for v_idx, pitch_base in enumerate((0x00, 0x01, 0x02)):
            self.voices[v_idx].pitch_base = pitch_base
        self.transpose_buffers = [0, 0, 0]  # $12ED,x per voice
        self.frame_idx = 0

        # Shared arranger state (single song-position across all voices,
        # per $10EB / $10D9 / $14EC / $14ED in the V0 row-advance band):
        self.arranger_idx = 0  # $10EB current song-position
        self.silence_flag = 0x80  # $10D9 -- init to $80 per $1572
        self.jp_armed_row = 0xFF  # $14EC -- "remembered FF row"
        self.jp_count = 0  # $14ED -- jump-count countdown

        # Global filter state.
        self.filter_res_routing = 0  # $10AA -> $D417
        self.volume_mode = 0x0F  # $10AF -> $D418

        # Filter cutoff slide accumulator (per $10B5-$10D7).
        # The per-frame oscillator walks a 16-bit accumulator
        # ($10B6 lo / $10BE hi) by signed steps ($10B9 lo / $10C0 hi).
        # Direction is chosen by patching the ADC opcode at $10B8/$10BF
        # between $69 (ADC imm) and $E9 (SBC imm). After the high-byte
        # update a saturation/clamp + optional ASL produces $D416.
        self.cutoff_acc_lo = 0  # $10B6
        self.cutoff_acc_hi = 0  # $10BE
        self.cutoff_step_lo = 0  # $10B9
        self.cutoff_step_hi = 0  # $10C0
        self.cutoff_extra = 0  # $10CA (additional add at $10C9)
        self.cutoff_op_is_adc = True  # True = $10B8/$10BF patched ADC; False = SBC
        # $14EE startup-RNG patches at $10CE / $10D4. Bit 0 of V3-osc
        # entropy at boot: clear -> $10CE=$02, $10D4=$EA (NOP); set ->
        # $10CE=$00, $10D4=$0A (ASL). Hardcoded to the bit-0-clear
        # branch ($D416=$02 idle, no ASL on output).
        self.cutoff_floor = 0x02  # $10CE
        self.cutoff_output_asl = False  # $10D4 = NOP
        # Cached $D416 output, updated by every ``_cutoff_slide_step``
        # call. For sub_frame_count>=2, emit reads this (populated by
        # _sub_frame_update's per-NMI _cutoff_slide_step()). For
        # sub_frame_count==1, emit calls _cutoff_slide_step() directly.
        self.cutoff_d416_emit = self.cutoff_floor

        # Optional per-tick cascade trace (opt-in, off by default to keep
        # the hot loop fast). When ``trace_cascade`` is True, every
        # ``_sidcall_tick`` invocation appends a record describing the
        # cascade state to ``self.cascade_trace``.
        self.trace_cascade: bool = False
        self.cascade_trace: list[dict] = []

        # Optional per-NMI state trace (opt-in). When ``trace_per_nmi``
        # is True, every ``_sub_frame_update`` call appends a state
        # snapshot to ``self.per_nmi_snapshots`` at the NMI-ENTRY
        # point (= BEFORE the cutoff/cascade/pitch advance runs).
        self.trace_per_nmi: bool = False
        self.per_nmi_snapshots: list[dict] = []

    # ---- public API ---------------------------------------------------

    def play_frame(self) -> list[tuple[int, int]]:
        """Run one IRQ tick. Returns the ``(reg, value)`` SID register
        writes for the main-frame's last-write-per-register window.

        Per-NMI emit/update model:

          defMON's $0AED NMI handler fires every $715A/$715B cycles =
          $715C sub-frame NMIs per main player tick. Each NMI runs the
          body at $1022 (SID writes from operand bytes set by the
          PRIOR NMI's cascade + pitch slide) followed by $12DF-$1404
          cascade arms and $1405-$14EB pitch slide. Main-tick NMIs
          additionally run $1147 row-advance (arranger + per-voice
          pattern advance) BEFORE the cascade.

          play_frame layout per main frame (N >= 2):

            sub-frame update × (N - 1)  -- bin NMIs 0..N-2; main-tick
                                          at idx 1
            emit  (state = end of bin NMI N-2's body = NMI N-1 ENTRY)
            sub-frame update  -- bin NMI N-1

          For N == 1: 1 NMI per main frame, so the emit-shift
          collapses to "emit at start, then main-tick + 1 NMI body"."""
        N = self.sub_frame_count

        if N == 1:
            # 1 NMI per main frame: emit at start with state from end
            # of prior frame, then advance.
            writes = self._emit_frame_writes(cutoff_advance=True)
            self._main_tick_extra()
            self._sub_frame_update()
        else:
            # N >= 2: emit-shift. Run N-1 sub_frame_updates BEFORE
            # emit, with main-tick at idx 1. Split NMI N-1's body at
            # the emit position: $D416 (last in NMI body) captures
            # post-slide state; the SID-write band ($D40x) captures
            # NMI N-1 ENTRY state (mirroring the per-NMI body order:
            # $1022 SID writes use entry state, $10AC filter slide
            # advances + STA $D416 uses post-advance state).
            for k in range(N - 1):
                if k == 1:
                    self._main_tick_extra()
                self._sub_frame_update()
            # NMI N-1 split: snapshot (entry state), cutoff slide
            # advance (= NMI N-1's filter slide step, updates
            # cutoff_d416_emit cache), emit (non-cutoff regs read
            # entry state; $D416 reads post-slide cache), then rest
            # of NMI N-1's body (cascade + pitch + PS, no cutoff).
            if self.trace_per_nmi:
                self.per_nmi_snapshots.append(self._snapshot_per_nmi())
            self._cutoff_slide_step()
            writes = self._emit_frame_writes(cutoff_advance=False)
            if N == 2:
                # For sub_frame_count==2 the loop only iterates k=0
                # (= NMI 0 sub-frame), so the k==1 main-tick branch
                # never fires; NMI 1 IS the post-loop NMI and IS the
                # main-tick (every $715C-th NMI = every 2nd NMI for
                # N=2, fired at bin NMI 1 in 0-indexed). Run
                # row-advance between the filter-slide-and-emit half
                # of the body and the cascade+pitch half (body order:
                # filter slide -> $1147 row advance -> $12DF cascade
                # -> $1405 pitch).
                self._main_tick_extra()
            self._sub_frame_update(body_only=True)

        self.frame_idx += 1
        return writes

    def _emit_frame_writes(self, cutoff_advance: bool) -> list[tuple[int, int]]:
        """Build the (reg, value) SID-write list for this play_frame
        emit position. ``cutoff_advance=True`` triggers the emit-time
        _cutoff_slide_step() advance (N=1 path). ``False`` reads
        cached ``self.cutoff_d416_emit`` (N>=2 path; advances run in
        _sub_frame_update)."""
        writes: list[tuple[int, int]] = []
        for v_idx, v in enumerate(self.voices):
            base_pw = SID_REG_BASE + v_idx * 7 + 2
            base_f = SID_REG_BASE + v_idx * 7 + 0
            base_sr = SID_REG_BASE + v_idx * 7 + 6
            base_ad = SID_REG_BASE + v_idx * 7 + 5
            base_ctrl = SID_REG_BASE + v_idx * 7 + 4
            writes.append((base_pw + 0, v.pulse_lo))
            writes.append((base_pw + 1, v.pulse_hi))
            writes.append((base_f + 0, v.freq_lo))
            writes.append((base_f + 1, v.freq_hi))
            writes.append((base_sr, v.sr))
            writes.append((base_ad, v.ad))
            writes.append((base_ctrl, (v.ctrl_main ^ v.ctrl_eor) & 0xFF))
        writes.append((SID_REG_BASE + 0x17, self.filter_res_routing & 0xFF))
        writes.append((SID_REG_BASE + 0x18, (self.volume_mode & 0xF0) | 0x0F))
        if cutoff_advance:
            writes.append((SID_REG_BASE + 0x16, self._cutoff_slide_step()))
        else:
            writes.append((SID_REG_BASE + 0x16, self.cutoff_d416_emit))
        return writes

    # ---- per-NMI helpers ---------------------------------------------

    def _sub_frame_update(self, body_only: bool = False) -> None:
        """One NMI's worth of $1022-body advance: filter cutoff slide,
        then cascade arms, then pitch oscillator + PS sweep. Models
        the $10AC-$1405 band that fires on every NMI.

        Order: filter slide ($10AC-$10D7) is at the start of the
        body, BEFORE cascade ($12DF) and pitch ($1405).

        For ``sub_frame_count >= 2`` the cutoff slide fires per
        sub_frame_update (= per NMI). For ``sub_frame_count == 1``
        the cutoff slide is advanced by the emit-time call instead;
        per-NMI rate equals emit-time rate at N=1 so no information
        is lost.

        Snapshots state at NMI ENTRY (before any advance) when
        ``trace_per_nmi`` is enabled.

        ``body_only=True`` skips the per-NMI-entry snapshot AND the
        cutoff slide advance, running only cascade + pitch + PS.
        Used by the NMI N-1 split (play_frame): the snapshot + cutoff
        advance run before the emit (so $D416 captures NMI N-1's
        post-slide state) and the cascade/pitch/PS run AFTER the
        emit. The split keeps total cutoff advances per main frame
        at sub_frame_count without double-counting."""
        if not body_only:
            if self.trace_per_nmi:
                self.per_nmi_snapshots.append(self._snapshot_per_nmi())
            if self.sub_frame_count >= 2:
                self._cutoff_slide_step()
        for v_idx in range(3):
            self._sidcall_tick(v_idx, layer=1)
            self._sidcall_tick(v_idx, layer=2)
        for v_idx in (2, 1, 0):
            self._pitch_slide_voice(v_idx)
            self._ps_voice(v_idx)

    def _snapshot_per_nmi(self) -> dict:
        """Build a per-NMI state snapshot at NMI ENTRY (state BEFORE
        this NMI's body runs).

        Field groups: ``pw`` (V0/V1/V2 pw_lo/pw_hi), ``ps``
        (v?_ps_depth), ``row`` (v?_sc?_row_idx), ``step``
        (v?_sc?_step), ``cutoff`` (acc_lo/hi, op_lo/hi_opcode,
        step_lo/hi, extra, floor, asl_opcode), ``extra``
        (silence_latch, arranger_row)."""
        v0, v1, v2 = self.voices
        return {
            "frame_idx": self.frame_idx,
            "nmi_idx_in_frame": len(self.per_nmi_snapshots) % self.sub_frame_count,
            # PW operand slots
            "v0_pw_lo": v0.pulse_lo & 0xFF,
            "v0_pw_hi": v0.pulse_hi & 0xFF,
            "v1_pw_lo": v1.pulse_lo & 0xFF,
            "v1_pw_hi": v1.pulse_hi & 0xFF,
            "v2_pw_lo": v2.pulse_lo & 0xFF,
            "v2_pw_hi": v2.pulse_hi & 0xFF,
            # PS depth
            "v0_ps_depth": v0.ps_depth & 0xFF,
            "v1_ps_depth": v1.ps_depth & 0xFF,
            "v2_ps_depth": v2.ps_depth & 0xFF,
            # Cascade row indices
            "v0_sc1_row_idx": v0.sc1_row_idx & 0xFF,
            "v1_sc1_row_idx": v1.sc1_row_idx & 0xFF,
            "v2_sc1_row_idx": v2.sc1_row_idx & 0xFF,
            "v0_sc2_row_idx": v0.sc2_row_idx & 0xFF,
            "v1_sc2_row_idx": v1.sc2_row_idx & 0xFF,
            "v2_sc2_row_idx": v2.sc2_row_idx & 0xFF,
            # Cascade step counters
            "v0_sc1_step": v0.sc1_step_counter & 0xFF,
            "v1_sc1_step": v1.sc1_step_counter & 0xFF,
            "v2_sc1_step": v2.sc1_step_counter & 0xFF,
            "v0_sc2_step": v0.sc2_step_counter & 0xFF,
            "v1_sc2_step": v1.sc2_step_counter & 0xFF,
            "v2_sc2_step": v2.sc2_step_counter & 0xFF,
            # Env-program operand slots (ctrl_main / ctrl_eor / ad / sr).
            # These are the bytes the $1022 SID-write band reads at NMI
            # entry to emit $D404-$D406 (V0), $D40B-$D40D (V1),
            # $D412-$D414 (V2). Patched by $16B0 sidtab_row_apply on
            # cascade-fire NMI.
            "v0_ctrl": v0.ctrl_main & 0xFF,
            "v0_ctrl_eor": v0.ctrl_eor & 0xFF,
            "v0_ad": v0.ad & 0xFF,
            "v0_sr": v0.sr & 0xFF,
            "v1_ctrl": v1.ctrl_main & 0xFF,
            "v1_ctrl_eor": v1.ctrl_eor & 0xFF,
            "v1_ad": v1.ad & 0xFF,
            "v1_sr": v1.sr & 0xFF,
            "v2_ctrl": v2.ctrl_main & 0xFF,
            "v2_ctrl_eor": v2.ctrl_eor & 0xFF,
            "v2_ad": v2.ad & 0xFF,
            "v2_sr": v2.sr & 0xFF,
            # Cutoff slide state
            "cutoff_acc_lo": self.cutoff_acc_lo & 0xFF,
            "cutoff_acc_hi": self.cutoff_acc_hi & 0xFF,
            "cutoff_step_lo": self.cutoff_step_lo & 0xFF,
            "cutoff_step_hi": self.cutoff_step_hi & 0xFF,
            "cutoff_op_lo_opcode": 0x69 if self.cutoff_op_is_adc else 0xE9,
            "cutoff_op_hi_opcode": 0x69 if self.cutoff_op_is_adc else 0xE9,
            "cutoff_extra": self.cutoff_extra & 0xFF,
            "cutoff_floor": self.cutoff_floor & 0xFF,
            "cutoff_asl_opcode": 0x0A if self.cutoff_output_asl else 0xEA,
            # Extras
            "silence_latch": self.silence_flag & 0xFF,
            "arranger_row": self.arranger_idx & 0xFF,
        }

    def _main_tick_extra(self) -> None:
        """Run the main-tick-only $1147 row-advance band. Only the
        main-tick NMI ($1003 entry) visits $1147; sub-frame NMIs
        ($1006 entry) skip it via the $10D8 RTS patch.

        defMON's $10DA `BPL $1147` falls through to $10DC's silence
        handler when silence_flag bit 7 is set, which zeros row
        timers then walks the arranger AND falls through to $1147
        on the SAME main-tick NMI. The silence-flag branch must
        run both `_arranger_advance` AND `_pattern_advance` for each
        voice; otherwise frame 0 misses the first $1147 pass and
        the cascade fire lags by one main-tick group.
        """
        if self.silence_flag & 0x80:
            self._arranger_advance()
        for v_idx in range(3):
            self._pattern_advance(v_idx)

    # ---- filter cutoff slide oscillator ($10B5-$10D7) ----------------

    def _cutoff_slide_step(self) -> int:
        """Update the 16-bit cutoff slide accumulator and return the
        $D416 value to emit. Models the carry-propagating
        ADC/SBC pair at $10B5-$10C0, the BPL clamp at $10C1, the
        ADC #$10CA at $10C9, the BMI/CMP-#$02 saturation logic, and
        the optional ASL output (patched by $14EE)."""
        carry = 0  # CLC at $10B7
        # Low byte update ($10B8 = ADC or SBC).
        if self.cutoff_op_is_adc:
            raw = self.cutoff_acc_lo + self.cutoff_step_lo + carry
            new_lo = raw & 0xFF
            carry = 1 if raw > 0xFF else 0
        else:
            raw = self.cutoff_acc_lo - self.cutoff_step_lo - (1 - carry)
            new_lo = raw & 0xFF
            carry = 1 if raw >= 0 else 0
        self.cutoff_acc_lo = new_lo
        # High byte update ($10BF = ADC or SBC).
        if self.cutoff_op_is_adc:
            raw = self.cutoff_acc_hi + self.cutoff_step_hi + carry
            new_hi = raw & 0xFF
            high_carry = 1 if raw > 0xFF else 0
        else:
            raw = self.cutoff_acc_hi - self.cutoff_step_hi - (1 - carry)
            new_hi = raw & 0xFF
            high_carry = 1 if raw >= 0 else 0
        # $10C1 BPL $10C6: if positive keep A; if negative reload from
        # $10CE. Neither path touches the carry flag (LDA $10CE is just
        # a load), so high_carry survives into the next ADC.
        if new_hi & 0x80:
            new_hi = self.cutoff_floor
        self.cutoff_acc_hi = new_hi
        # $10C9 ADC #$10CA. Uses the carry from the high update.
        raw = new_hi + self.cutoff_extra + high_carry
        a = raw & 0xFF
        # $10CB BMI $10D1 / $10CD CMP #$02 / $10CF BCS $10D4. The CMP
        # sets a fresh carry: C=1 iff a >= 2. The BCS uses that. So the
        # final value is:
        #   - a bit 7 set        -> A := floor
        #   - else a >= 2        -> A unchanged
        #   - else (a in [0, 1]) -> A := floor
        if (a & 0x80) or a < 2:
            a = self.cutoff_floor
        if self.cutoff_output_asl:
            a = (a << 1) & 0xFF
        # Cache for the next emit. play_frame uses self.cutoff_d416_emit
        # rather than calling this method again -- so total advances per
        # main frame = number of _sub_frame_update calls
        # (= sub_frame_count).
        self.cutoff_d416_emit = a
        return a

    # ---- ram access helpers ------------------------------------------

    # ---- pitch slide oscillator ($1405) -------------------------------

    def _pitch_slide_voice(self, v_idx: int) -> None:
        """Apply one voice's pitch update per the $1405 body.

        Modes (from $101B,x = ``slide_mode``):
          * 0 -- no slide: ``freq = NOTE_PITCH[note] + pitch_base``.
          * $80..$FF (negative) -- active slide; ``Y = slide_mode << 1``
            indexes into the slide-rate LUTs at $14F8 / $1594 (negative
            direction when ``slide_mode`` bit 6 set) or $1578 / $1614
            (positive direction otherwise). Slide accumulator at
            $1019/$101A,x integrates, then added to NOTE_PITCH[note] to
            produce the final freq.
          * $01..$7F (positive) -- portamento toward
            (note + slide_mode); adds a one-frame interval step
            (adjacent-semitone delta at the target) to
            NOTE_PITCH[current_note]. Step is NOT integrated across
            frames; only animates when current_note changes (sidTAB
            TR mid-slide). See [region."$1469"] in annotations.toml
            for the 6502 reference.
        """
        v = self.voices[v_idx]
        slide_mode = v.slide_mode & 0xFF
        note = v.current_note & 0x7F

        if slide_mode == 0:
            # Simple lookup path ($1453-$1466).
            pitch_lo = NOTE_PITCH_LO[note]
            pitch_hi = NOTE_PITCH_HI[note]
            total_lo = pitch_lo + (v.pitch_base & 0xFF)
            carry = total_lo >> 8
            v.freq_lo = total_lo & 0xFF
            v.freq_hi = (pitch_hi + carry) & 0xFF
            return

        if not (slide_mode & 0x80):
            # Portamento path ($1469+). Computes a single-frame step
            # toward target = current_note + slide_mode using the
            # adjacent-semitone interval LUTs at $1583/$1584 (lo) and
            # $161F/$1620 (hi), then adds that step once to
            # NOTE_PITCH[current_note]. Step is NOT integrated across
            # frames -- portamento only animates when current_note
            # changes (e.g., sidTAB TR mid-slide). Reference:
            # annotations.toml [region."$1469"] / defmon.s _1469.
            target = (slide_mode + note) & 0xFF

            # step_lo = $1584,Y - $1583,Y. SEC at $1471 -> C=1 input,
            # so 6502 SBC reduces to clean (A - M).
            a_lo = _lut(0x1584, target)
            m_lo = _lut(0x1583, target)
            sub_lo = a_lo - m_lo
            step_lo = sub_lo & 0xFF
            c_out_lo = 1 if sub_lo >= 0 else 0  # 6502 SBC C-out

            # step_hi = $1620,Y - $161F,Y - (1 - C_in). Borrow chains
            # from the prior SBC at $1472.
            a_hi = _lut(0x1620, target)
            m_hi = _lut(0x161F, target)
            sub_hi = a_hi - m_hi - (1 - c_out_lo)
            step_hi = sub_hi & 0xFF

            # freq_lo = NOTE_PITCH_LO[note] + step_lo  (CLC at $1487).
            pitch_lo = NOTE_PITCH_LO[note]
            pitch_hi = NOTE_PITCH_HI[note]
            total_lo = pitch_lo + step_lo
            carry = total_lo >> 8
            v.freq_lo = total_lo & 0xFF
            # freq_hi = NOTE_PITCH_HI[note] + step_hi + carry.
            v.freq_hi = (pitch_hi + step_hi + carry) & 0xFF
            return

        # Active slide ($140E+). Y = slide_mode << 1 (low 8 bits).
        # Sign of Y picks the direction LUT pair:
        #   Y bit 7 == 0 (slide_mode in $80..$BF) -> positive slide
        #   Y bit 7 == 1 (slide_mode in $C0..$FF) -> negative slide
        y = (slide_mode << 1) & 0xFF
        if y & 0x80:
            # Negative slide: SBC slide_dec_{lo,hi}[Y] from accumulator.
            dec_lo = _lut(SLIDE_DEC_LO_BASE, y)
            dec_hi = _lut(SLIDE_DEC_HI_BASE, y)
            # 6502 SBC with carry assumed set (defMON's path uses SEC
            # implicitly via the prior ASL clearing carry).
            # Two-byte subtract with borrow.
            lo = v.slide_accum_lo - dec_lo
            borrow = 1 if lo < 0 else 0
            v.slide_accum_lo = lo & 0xFF
            v.slide_accum_hi = (v.slide_accum_hi - dec_hi - borrow) & 0xFF
        else:
            # Positive slide: ADC slide_inc_{lo,hi}[Y] into accumulator.
            inc_lo = _lut(SLIDE_INC_LO_BASE, y)
            inc_hi = _lut(SLIDE_INC_HI_BASE, y)
            lo = v.slide_accum_lo + inc_lo
            carry = 1 if lo > 0xFF else 0
            v.slide_accum_lo = lo & 0xFF
            v.slide_accum_hi = (v.slide_accum_hi + inc_hi + carry) & 0xFF

        # Add NOTE_PITCH[current_note] to accumulator for final freq
        # (body at $143A-$144D).
        pitch_lo = NOTE_PITCH_LO[note]
        pitch_hi = NOTE_PITCH_HI[note]
        lo = v.slide_accum_lo + pitch_lo
        carry = 1 if lo > 0xFF else 0
        v.freq_lo = lo & 0xFF
        v.freq_hi = (v.slide_accum_hi + pitch_hi + carry) & 0xFF

    def _ps_voice(self, v_idx: int) -> None:
        """Apply one voice's pulse-width sweep per $1495-$14E2.

        Body (X = voice index, $1023/$1025,x = PW lo/hi patch slots,
        $101E,x = depth + direction bit)::

            $1495 LDY $101E,X         ; depth
            $1498 BEQ $14E3           ; depth == 0 -> no-op
            $149A BPL $14D0           ; bit 7 clear -> SUB path

            ; ADD path ($149C-):
            $149C TYA / ANC #$7F      ; A := depth & $7F; C := 0
            $149F ADC $1023,X / STA   ; pulse_lo += magnitude
            $14A5 BCC $14E3           ; no carry -> done
            $14A7 LDA $1025,X / CMP #$0F / BEQ $14B4
            $14AE INC $1025,X / JMP $14E3  ; bump pulse_hi
            $14B4 LDA #$F8 / STA $1023,X   ; clamp lo = $F8
            $14B9 TYA / EOR #$80 / STA $101E,X  ; flip direction to SUB
            $14BF JMP $14E3

            ; SUB underflow handler ($14C2-):
            $14C2 LDA #$01 / STA $1023,X   ; clamp lo = $01
            $14C7 TYA / EOR #$80 / STA $101E,X  ; flip direction to ADD
            $14CD JMP $14E3

            ; SUB path ($14D0-):
            $14D0 LDA $1023,X / SBC $101E,X / STA $1023,X
            $14D9 BCS $14E3           ; no borrow -> done
            $14DB LDA $1025,X / BEQ $14C2  ; pulse_hi == 0 -> underflow
            $14E0 DEC $1025,X / JMP $14E3

        Carry-in to the ADD path's ADC is forced to 0 by the
        ``ANC #$7F`` (undocumented opcode: ``A := A AND M; C := bit 7
        of result``, which is always 0 here). Carry-in to the SUB
        path's SBC is *uncontrolled* -- it inherits whatever the
        prior pitch-slide ADC ($144A or $145A) left. For
        ``pitch_base == 0`` (the common case), the no-slide path's
        ADC #$00 leaves C unchanged from whatever entered the
        oscillator; we approximate as 0, which costs the SUB path one
        extra unit per frame compared to a SEC-then-SBC implementation.
        """
        v = self.voices[v_idx]
        depth = v.ps_depth & 0xFF
        if depth == 0:
            return

        if depth & 0x80:
            # ADD path. C-in := 0 (set by ANC #$7F).
            magnitude = depth & 0x7F
            total = v.pulse_lo + magnitude
            v.pulse_lo = total & 0xFF
            if total <= 0xFF:
                return  # BCC exit
            # Carry out: increment pulse_hi (or clamp + flip direction).
            if (v.pulse_hi & 0xFF) == 0x0F:
                v.pulse_lo = 0xF8
                v.ps_depth = depth ^ 0x80  # flip to SUB
            else:
                v.pulse_hi = (v.pulse_hi + 1) & 0xFF
            return

        # SUB path. C-in is uncontrolled in defMON (no SEC before SBC
        # at $14D3); model the literal disassembly with C-in = 0 (the
        # carry left by the prior pitch-slide ADC at $144A/$145A when
        # pitch_base == 0).
        diff = v.pulse_lo - depth - 1
        v.pulse_lo = diff & 0xFF
        if diff >= 0:
            return  # BCS exit
        # Borrow out: decrement pulse_hi (or clamp + flip direction).
        if v.pulse_hi == 0:
            v.pulse_lo = 0x01
            v.ps_depth = depth ^ 0x80  # flip to ADD
        else:
            v.pulse_hi = (v.pulse_hi - 1) & 0xFF

    def _ram(self, addr: int) -> int:
        return self.snapshot[(addr - LOAD_ADDRESS) & 0xFFFF]

    def _ram_write(self, addr: int, val: int) -> None:
        self.snapshot[(addr - LOAD_ADDRESS) & 0xFFFF] = val & 0xFF

    # ---- pattern advance ----------------------------------------------

    def _arranger_advance(self) -> None:
        """Run the $10EA-$1144 arranger walk: skip $00 (silent-voice)
        and $FF (jump) entries on V0's arranger, resolve to a row whose
        V0 entry is a real pattern number, then set up pat_base for
        each voice from that row. Resets row timers across all voices
        to ``silence_flag & $0F`` (= 0 at startup, $0F post-EOP),
        clears silence_flag to its positive lownibble form, and bumps
        arranger_idx by 1 past the resolved row."""
        # Resolve silence-flag lownibble first; this is what $114A,
        # $11D2, $125A get set to.
        new_timer = self.silence_flag & 0x0F
        self.silence_flag = new_timer

        # Walk arranger from current arranger_idx until we hit a row
        # whose V0 entry is a non-$FF, non-$00 pattern number.
        idx = self.arranger_idx
        for _guard in range(512):
            v0_entry = self._ram(0x1B00 + idx)
            if v0_entry == 0xFF:
                # $FF = jump command. V1[idx] = target row, V2[idx] = count
                # ($1B00,Y entry being $FF triggers the $10EF BPL skip
                # at $10EC LDX, falling into the V2-arranger inspection
                # at $10F1+).
                target = self._ram(0x1C00 + idx)
                count = self._ram(0x1D00 + idx)
                if count == 0:
                    # Immediate jump every visit ($10F4 BEQ $1112).
                    idx = target & 0xFF
                    continue
                if self.jp_armed_row == idx:
                    # We've been here before. DEC the count; if it
                    # underflows, fall through past the FF row (= idx+1).
                    self.jp_count = (self.jp_count - 1) & 0xFF
                    if self.jp_count == 0:
                        self.jp_armed_row = 0xFF  # disarm
                        idx = (idx + 1) & 0xFF
                        continue
                    # Still has jumps left; take the jump.
                    idx = target & 0xFF
                    continue
                # First visit -- arm and take the jump.
                self.jp_armed_row = idx
                self.jp_count = count
                idx = target & 0xFF
                continue
            if v0_entry == 0:
                # V0 silent on this row but other voices may have
                # patterns. defMON DOES read all three voice pat_nums at
                # the same arranger row regardless of V0=0; we skip
                # only on full triplet-zero. (Approximation: tracker
                # arrangements typically have V0=0 rows that are
                # entirely silent across voices; treat as advance.)
                if self._ram(0x1C00 + idx) == 0 and self._ram(0x1D00 + idx) == 0:
                    idx = (idx + 1) & 0xFF
                    continue
            break
        else:
            return

        # idx now points at the active arranger row. Set up pat_base
        # for each voice from voice's own arranger entry at this row.
        for v_idx in range(3):
            arr_base = ARRANGER_BASE[v_idx]
            pat_num = self._ram(arr_base + idx) & 0x7F  # mask high bit
            pat_base = PATTERN_BANK_BASE + pat_num * PATTERN_STRIDE
            v = self.voices[v_idx]
            v.pat_base_lo = pat_base & 0xFF
            v.pat_base_hi = (pat_base >> 8) & 0xFF
            v.pattern_step_idx = 0
            v.row_timer = new_timer

        # Bump arranger_idx past the resolved row (per $1143 INY).
        self.arranger_idx = (idx + 1) & 0xFF

    def _pattern_advance(self, v_idx: int) -> None:
        v = self.voices[v_idx]
        if v.row_timer > 0:
            v.row_timer -= 1
            return

        # Row read: read 4 bytes from voice's pat_base + step*4.
        pat_base = (v.pat_base_hi << 8) | v.pat_base_lo
        step = v.pattern_step_idx
        flag = self._ram(pat_base + step * 4 + 0)
        slot_a = self._ram(pat_base + step * 4 + 1)
        slot_b = self._ram(pat_base + step * 4 + 2)
        note = self._ram(pat_base + step * 4 + 3)

        if flag & FLAG_GATE_A:
            v.sc1_row_idx = slot_a
            v.sc1_step_counter = 0  # = "fetch now" on next _sidcall_tick
        if flag & FLAG_GATE_B:
            v.sc2_row_idx = slot_b
            v.sc2_step_counter = 0
        if flag & FLAG_GATE_N:
            # Per $11A1-$11B0 in the row-advance BMI branch: GATE_N
            # stores the pattern's NOTE byte verbatim to both $12ED
            # (transpose buffer) and $137F (current_note). Subsequent
            # sidTAB row TR columns add to $12ED to produce a new
            # current_note. So the transpose buffer carries the raw
            # GATE_N note byte forward, NOT zero.
            v.current_note = note & 0x7F
            self.transpose_buffers[v_idx] = note & 0x7F
            # GATE_N unconditionally zeros the per-voice slide state
            # ($1019, $101A, $101B) so a fresh note never inherits the
            # prior row's slide accumulator.
            v.slide_accum_lo = 0
            v.slide_accum_hi = 0
            v.slide_mode = 0

        if flag & FLAG_ALT:
            # ALT signals end-of-pattern. Per $11B4 LDX #flag / $11B6
            # BPL skip / $11B8 STX $10D9: X = full flag byte (ALT + dur
            # in lownibble), STX writes flag verbatim to $10D9. Next
            # frame, $10D9 bit 7 set triggers arranger advance, and
            # arranger advance sets all row timers := $10D9 & $0F =
            # ALT row's dur. Storing $FF here (as before) made the
            # inter-pattern gap always 15 frames instead of the
            # composer-chosen dur value.
            self.silence_flag = flag
            # Pattern_step_idx is reset to 0 by _arranger_advance next
            # frame. Don't advance it here.
        else:
            v.pattern_step_idx = (step + 1) % STEPS_PER_PATTERN

        # row_timer := dur + 1: 2-iteration dec loop plus the
        # initial fire frame = 3 frames held at dur=1.
        v.row_timer = (flag & FLAG_DUR_MASK) + 1

    # ---- sidcall cascade ---------------------------------------------

    def _sidcall_tick(self, v_idx: int, layer: int) -> None:
        """One frame's sidcall tick per $12DF (V0 sidcall1) body:

            LDA #counter         ; counter = operand at $12E0 (et al)
            BEQ refetch          ; 0 -> fetch a new row
            BMI skip             ; $80..$FF -> skip (frozen / STop)
            DEC counter
            (skip:)

        So counter == 0 fires a refetch; counter in 1..$7F decrements
        every frame and refetches on underflow-to-0; counter in
        $80..$FF (sign bit set) skips entirely. Initial counter = $FF
        from player_init keeps the cascade silent until GATE_A/B in a
        pattern row sets counter := 0 (= "fetch now").
        """
        v = self.voices[v_idx]
        if layer == 1:
            counter = v.sc1_step_counter
        else:
            counter = v.sc2_step_counter

        if counter & 0x80:
            # BMI skip (frozen / STop).
            return

        if counter != 0:
            # Holding: decrement and exit.
            if layer == 1:
                v.sc1_step_counter -= 1
            else:
                v.sc2_step_counter -= 1
            return

        # counter == 0: fetch + apply.
        row_idx = v.sc1_row_idx if layer == 1 else v.sc2_row_idx

        # JP redirect ($12EE-$12F9): if $1900,row_idx == 0, this row
        # is a jump marker. The actual row to apply is $1800,row_idx
        # (= target row index), and the next-row index becomes
        # target+1 (after the INY at $1307). The redirect happens
        # once per fetch -- no loop check, so if target itself has
        # $1900 == 0, the player reads target's $1800 too without
        # further redirect (the LDA at $12F9 just stores to $FC).
        # Without this, the cascade walks straight through what defMON
        # author intended as a delay-loop, reaching the STop row and
        # gating the voice on indefinitely (V2 over-gating in
        # .GLOW WORM frames 0..500).
        if self._ram(0x1900 + row_idx) == 0:
            row_idx = self._ram(0x1800 + row_idx)

        row_addr = SIDTAB_DATA_BASE + row_idx * SIDTAB_ROW_STRIDE
        row_off = row_addr - LOAD_ADDRESS
        raw = bytes(self.snapshot[row_off : row_off + SIDTAB_ROW_STRIDE])

        # Read DL byte at $1E00,row_idx BEFORE applying the row -- it
        # tells us how long to hold or whether to STop.
        dl = self._ram(0x1E00 + row_idx)

        # Apply the row to the voice's working slots ($16B0 model).
        self.transpose_buffers[v_idx] = _apply_sidtab_row(
            v, raw, transpose_buffer=self.transpose_buffers[v_idx]
        )

        # Apply second bitmap (high-half columns) if present. We
        # consume the bytes the low bitmap took, then walk the high
        # bitmap. Globals get written into player state.
        self._apply_high_bitmap_globals(v_idx, v, raw)

        # Now schedule the next fetch from DL. Per the cascade body
        # at $12DF-$130D: the DL byte ($1E00,Y) is stored verbatim to
        # the step counter at $12E0,X (LDA-imm operand inside the
        # `LDA #$FF; BEQ refetch; BMI skip; DEC counter` decision
        # block at $12DF). Sign matters:
        #   $00         -> refetch next frame (= 1-frame hold)
        #   $01..$7F    -> decrement each frame, refetch when 0
        #   $80..$FF    -> BMI skip: freeze cascade, no apply, no dec
        # STop ($80..$FF) does NOT touch ctrl_main / ctrl_eor; the
        # SID-write band keeps emitting `ctrl_main ^ ctrl_eor` every
        # frame from the frozen patch slots. Whether the gate is on
        # or off after STop is determined by the row's own WGh / WGl.
        # Store DL byte verbatim to the step counter (per the cascade
        # body $12E0 STA). DL & $80 = STop (next frame BMI skips);
        # DL in $01..$7F = hold-then-refetch; DL == $00 = refetch
        # next frame. Advance the row index regardless of DL sign.
        if layer == 1:
            v.sc1_step_counter = dl
            v.sc1_row_idx = (row_idx + 1) & 0xFF
        else:
            v.sc2_step_counter = dl
            v.sc2_row_idx = (row_idx + 1) & 0xFF

        if self.trace_cascade:
            # Decode the high-bitmap PS byte for this row (if any) — the
            # actual depth byte just applied to v.ps_depth via
            # _apply_high_bitmap_globals above.
            low_bm = raw[0]
            n_low = bin(low_bm).count("1")
            high_bm_off = 1 + n_low
            high_bm = raw[high_bm_off] if high_bm_off < len(raw) else 0
            ps_byte = None
            if high_bm & 0x80 and (high_bm_off + 1) < len(raw):
                ps_byte = raw[high_bm_off + 1]
            self.cascade_trace.append(
                {
                    "frame": self.frame_idx,
                    "voice": v_idx,
                    "layer": layer,
                    "fetched_row": row_idx,
                    "dl_loaded": dl,
                    "low_bm": low_bm,
                    "high_bm": high_bm,
                    "ps_byte": ps_byte,
                    "ps_depth_after": v.ps_depth,
                    "pulse_lo_after": v.pulse_lo,
                    "pulse_hi_after": v.pulse_hi,
                }
            )

    # ---- high-bitmap globals -----------------------------------------

    def _apply_high_bitmap_globals(self, v_idx: int, v: Voice, raw: bytes) -> None:
        """After the low bitmap walk, $16B0 reads the next byte as the
        high bitmap and dispatches PS/RE/FV/CP/ACID. We don't track
        the exact byte position the low walk consumed, so we re-parse
        via the ``SidtabRow`` logic (cheap; one row apply per cascade
        fetch)."""
        if not raw:
            return

        # $16B4 BEQ $1712: when the low bitmap byte is zero, defMON
        # jumps STRAIGHT to the high-bitmap walk at $1712 -- the
        # row still carries CP/ACID/RE/FV/PS bytes after a zero
        # low_bm, they live at offset 1 onwards. Only the byte
        # consumption from low fields differs.
        low_bm = raw[0]
        if low_bm == 0:
            pos = 1
        else:
            pos = 1
            for bit in (6, 7, 5, 4, 3, 2, 1):
                if low_bm & (1 << bit):
                    pos += 1
        if pos >= len(raw):
            return
        high_bm = raw[pos]
        pos += 1
        if high_bm == 0:
            return

        for bit, name, width in HIGH_BITMAP_ORDER:
            if not (high_bm & (1 << bit)):
                continue
            if pos + width > len(raw):
                return
            if width == 2:
                val = (raw[pos] << 8) | raw[pos + 1]
            else:
                val = raw[pos]
            pos += width

            if name == "PS":
                # Per $1495-$14E2: bit 7 of depth = direction (1 = add,
                # 0 = sub); low 7 bits = sweep magnitude per frame.
                # The PS oscillator runs every frame from $1495 (called
                # from the pitch slide oscillator at $1450/$1466 JMP);
                # depth value sticks until the sidTAB column rewrites it
                # or the clamp logic at $14B4/$14C2 flips bit 7.
                v.ps_depth = val & 0xFF
            elif name == "RE":
                # Three-way dispatch at $172D/$172F/$1741 + $1738:
                #   row_byte == 0        -> $10AA := $10AA & $1021,x
                #   row_byte bit 3 set   -> $10AA := ($10AA & $0F)
                #                             | row_byte | $1020,x
                #   else                 -> $10AA := row_byte
                # $1020,x / $1021,x are per-voice constants in the
                # working record. From the static image they are:
                #   V0: $1020=$01 $1021=$FE   (voice 1 filter route bit)
                #   V1: $1051=$02 $1052=$FD
                #   V2: $1082=$04 $1083=$FB
                voice_or = (0x01, 0x02, 0x04)[v_idx]
                voice_and = (0xFE, 0xFD, 0xFB)[v_idx]
                if val == 0:
                    self.filter_res_routing = self.filter_res_routing & voice_and
                elif val & 0x08:
                    self.filter_res_routing = (
                        (self.filter_res_routing & 0x0F) | val | voice_or
                    ) & 0xFF
                else:
                    self.filter_res_routing = val & 0xFF
            elif name == "FV":
                # $1759 STA $10AF directly. Per-frame: $D418 emit is
                # `$10AF & $F0 | $0F`. Store the row byte verbatim;
                # the emission masks low nibble.
                self.volume_mode = val & 0xFF
            elif name == "CP":
                # $1764 STA $10CA: the byte is stored verbatim into the
                # "additional add" operand at $10C9. Per-frame:
                # $D416 = clamp($10BE + $10CA + C_from_$10BF). Treat
                # signed -- the slide oscillator does signed arithmetic
                # via overflow into the BMI/CMP test.
                self.cutoff_extra = val & 0xFF
            elif name == "ACID":
                # 16-bit command, read as two bytes (low, high) at
                # $176D LAX / $1770 LDA. Dispatch on bit 7 of high:
                #   bit 7 clear:  absolute -- $10B6 := low, $10BE :=
                #                 high, $10B9 := 0, $10C0 := 0.
                #   bit 7 set:    slide. ASL high; if new sign set
                #                 (= old bit 6 set) -> SBC path,
                #                 else ADC path. Both store low ->
                #                 $10B9 and (high & $3F or $7F) ->
                #                 $10C0, plus patch $10B8/$10BF opcode.
                # The high_bm walker reads the bytes as
                # (raw[pos] << 8) | raw[pos+1] -- so low is the upper
                # byte of val, high is the lower byte.
                low = (val >> 8) & 0xFF
                high = val & 0xFF
                if not (high & 0x80):
                    self.cutoff_acc_lo = low
                    self.cutoff_acc_hi = high
                    self.cutoff_step_lo = 0
                    self.cutoff_step_hi = 0
                else:
                    self.cutoff_step_lo = low
                    # ASL high (carry from ASL discarded by the
                    # subsequent ALR/LSR). New sign bit = old bit 6 of
                    # high; that picks SBC vs ADC.
                    if high & 0x40:
                        # SBC path ($1796+). $179B reads A after the
                        # ALR #$7F at $1799, which leaves (high & $3F)
                        # in A. Opcode at $10B8/$10BF := $E9 (SBC).
                        self.cutoff_step_hi = high & 0x3F
                        self.cutoff_op_is_adc = False
                    else:
                        # ADC path ($1786+). $178A reads A after the
                        # LSR at $1789, which leaves (high & $7F) in A.
                        # Opcode := $69 (ADC).
                        self.cutoff_step_hi = high & 0x7F
                        self.cutoff_op_is_adc = True


# ----------------------------------------------------------------------
# Render driver -- emits WAV via pyresidfp.
# ----------------------------------------------------------------------


def render_wav(
    prg_path: Path,
    out_wav: Path,
    seconds: float = 60.0,
    model_name: str = "MOS6581",
    dedupe_writes: bool = True,
) -> int:
    try:
        from pyresidfp import SoundInterfaceDevice
        from pyresidfp.sound_interface_device import ChipModel  # type: ignore
    except ImportError as e:
        raise SystemExit(f"pyresidfp required: {e}") from e

    import numpy as np

    song = DefmonSong.from_file(str(prg_path))
    player = DefmonPlayer(song)

    sid = SoundInterfaceDevice(model=getattr(ChipModel, model_name))
    sid.reset()
    for r in range(25):
        sid.write_register(r, 0)  # type: ignore[arg-type]
    sid.clock(timedelta(seconds=0.05))

    cycles_per_sec = sid.clock_frequency
    seconds_per_frame = player.cycles_per_frame / cycles_per_sec
    n_frames = int(seconds / seconds_per_frame)

    # Post-reset every register reads zero; seed last_vals so the first
    # frame's no-op writes get suppressed too.
    last_vals = [0] * 25
    total_writes = 0
    skipped_writes = 0
    write_log: list[tuple[int, int, int]] = []  # (frame, reg, value)

    chunks: list = []
    for frame_idx in range(n_frames):
        for reg, val in player.play_frame():
            r = reg - SID_REG_BASE
            if 0 <= r <= 0x18:
                v = val & 0xFF
                total_writes += 1
                if dedupe_writes and last_vals[r] == v:
                    skipped_writes += 1
                    continue
                sid.write_register(r, v)  # type: ignore[arg-type]
                last_vals[r] = v
                write_log.append((frame_idx, r, v))
        samples = sid.clock(timedelta(seconds=seconds_per_frame))
        chunks.append(np.asarray(samples, dtype=np.int16))

    audio = np.concatenate(chunks) if chunks else np.array([], dtype=np.int16)
    sr = int(sid.sampling_frequency)
    with wave.open(str(out_wav), "wb") as w:
        # ``wave.open(..., "wb")`` returns Wave_write; pylint's static
        # inference treats the return as Wave_read and flags these.
        # pylint: disable=no-member
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(audio.tobytes())
    csv_path = out_wav.with_suffix(".csv")
    with open(csv_path, "w") as f:
        f.write("frame,reg,value\n")
        for frame_idx, r, v in write_log:
            f.write(f"{frame_idx},{r},{v}\n")
    msg = (
        f"wrote {out_wav}: {len(audio)} samples "
        f"({len(audio)/sr:.2f}s @ {sr}Hz, {model_name})"
    )
    if dedupe_writes:
        pct = (skipped_writes / total_writes * 100) if total_writes else 0.0
        msg += (
            f"; suppressed {skipped_writes}/{total_writes} duplicate "
            f"writes ({pct:.1f}%)"
        )
    msg += f"; wrote {csv_path}: {len(write_log)} write rows"
    print(msg)
    return 0


def dump_sid_writes(
    prg_path: Path,
    n_frames: int,
    out_jsonl: Path,
    cascade_trace: "Path | None" = None,
    per_nmi_trace: "Path | None" = None,
) -> int:
    """Run the player ``n_frames`` times and dump each frame's SID
    writes to ``out_jsonl`` (one JSON object per line).

    When ``cascade_trace`` is given, also emit a per-tick sidTAB
    cascade trace to that path (one JSON object per ``_sidcall_tick``
    invocation).

    When ``per_nmi_trace`` is given, also emit a per-NMI state trace
    (one JSON object per ``_sub_frame_update`` call, captured at NMI
    entry)."""
    song = DefmonSong.from_file(str(prg_path))
    player = DefmonPlayer(song)
    if cascade_trace is not None:
        player.trace_cascade = True
    if per_nmi_trace is not None:
        player.trace_per_nmi = True

    with open(out_jsonl, "w") as f:
        for frame_idx in range(n_frames):
            writes = player.play_frame()
            record = {
                "frame": frame_idx,
                "writes": [[reg, val] for reg, val in writes],
            }
            f.write(json.dumps(record) + "\n")
    print(
        f"wrote {out_jsonl}: {n_frames} frames "
        f"(cycles_per_frame={player.cycles_per_frame})"
    )

    if cascade_trace is not None:
        with open(cascade_trace, "w") as f:
            for rec in player.cascade_trace:
                f.write(json.dumps(rec) + "\n")
        print(f"wrote {cascade_trace}: {len(player.cascade_trace)} cascade events")

    if per_nmi_trace is not None:
        with open(per_nmi_trace, "w") as f:
            for rec in player.per_nmi_snapshots:
                f.write(json.dumps(rec) + "\n")
        print(
            f"wrote {per_nmi_trace}: {len(player.per_nmi_snapshots)} "
            f"per-NMI snapshots (sub_frame_count={player.sub_frame_count})"
        )
    return 0


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("prg", nargs="?", type=Path, help="defMON .prg")
    ap.add_argument("wav", nargs="?", type=Path, help="output WAV")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--model", default="MOS6581", choices=("MOS6581", "MOS8580"))
    ap.add_argument(
        "--dump-writes",
        type=Path,
        help="instead of rendering audio, dump per-frame SID writes to JSONL",
    )
    ap.add_argument(
        "--n-frames", type=int, default=600, help="number of frames to dump"
    )
    ap.add_argument(
        "--trace-cascade",
        type=Path,
        help="when set with --dump-writes, also emit per-tick sidTAB cascade "
        "events (fetched row, dl, ps_byte, ps_depth_after) to this JSONL",
    )
    ap.add_argument(
        "--per-nmi-trace",
        type=Path,
        help="when set with --dump-writes, also emit per-NMI player "
        "state (one JSON object per _sub_frame_update call, captured "
        "at NMI entry).",
    )
    ap.add_argument(
        "--dedupe-writes",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="suppress writes whose value matches the previous write to "
        "the same SID register (default: on; pass --no-dedupe-writes to "
        "disable).",
    )

    args = ap.parse_args(argv)

    if args.dump_writes is not None:
        if args.prg is None:
            ap.error("--dump-writes requires PRG")
        return dump_sid_writes(
            args.prg,
            args.n_frames,
            args.dump_writes,
            cascade_trace=args.trace_cascade,
            per_nmi_trace=args.per_nmi_trace,
        )

    if args.prg is None or args.wav is None:
        ap.error("PRG and WAV positional args required (or pass --dump-writes)")
    return render_wav(
        args.prg,
        args.wav,
        args.seconds,
        args.model,
        dedupe_writes=args.dedupe_writes,
    )


if __name__ == "__main__":
    sys.exit(main())
