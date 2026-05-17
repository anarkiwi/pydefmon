"""Reconstruct a defMON .prg from a defmon_player CSV write log, using
the snippet-and-JP algorithm.

Pipeline:

  A. Replay CSV -> per-frame target state for each voice + global.
  B. Slice each voice's timeline into "phrases" (boundary = note byte
     change or gate-bit edge).
  C. Cluster phrases by their (WGh, AD, SR, PW) initial-column tuple.
  D. Lay each instrument as a single sidTAB row with DL=$80 (STop).
  E. Per voice, emit pattern events at chunk-relative rel =
     (start_frame - 1) % CHUNK_FRAMES; chunks are CHUNK_FRAMES-frame
     song steps so all 3 voices share the arranger advance.
  F. Pad each pattern with delays to exactly fill CHUNK_FRAMES; end
     with ALT(dur=0 or 1).
  G. Linear arranger steps with set_jump back to 0; IRQ rate = 23546
     cycles (~41.84 Hz PAL).
  H. Serialize via DefmonSong.to_file().

Current approximations (lossy vs the input CSV):

  * Off-LUT freq writes (pitch slides) keep the prior note; round-trip
    misses the slide trajectory.
  * PW writes that break the 12-bit invariant get re-snapped; PS
    pulse-width sweeps are not reconstructed.
  * Cutoff sweeps emit only the player default $D416 = $02; ACID
    slide reconstruction not implemented.
  * RE column $D417 writes that need the bitmask path are skipped.

Round-trip on glow_worm.csv (2026-05-17): 832/14236 exact (frame,reg,
value) matches = 5.8%. Voice freq/CTRL/AD/SR on LUT-aligned notes
reproduce exactly; gaps are the unimplemented slide reconstruction
work listed above. See AGENTS.md Resolved (2026-05-17) for the
recall-by-register breakdown and the prioritised next-step backlog.

Usage: ``python3 -m pydefmon.dump_to_defmon CSV PRG``
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from pydefmon.defmon import (
    LOAD_ADDRESS,
    DefmonSong,
    PatternEvent,
    SidtabRow,
)
from pydefmon.defmon_player import (
    NOTE_PITCH_HI,
    NOTE_PITCH_LO,
    SLIDE_DEC_HI_BASE,
    SLIDE_DEC_LO_BASE,
    SLIDE_INC_HI_BASE,
    SLIDE_INC_LO_BASE,
    _lut,
)

PITCH_BASE = (0x00, 0x01, 0x02)
SIDTAB_DATA_BASE = 0x5F00
SIDTAB_ROW_STRIDE = 15
DEFAULT_CYCLES_PER_FRAME = 23546


def _build_slide_deltas() -> dict[int, int]:
    """slide_mode (in $80..$FF) → signed 16-bit per-frame freq delta.
    Drives both the forward (test) and inverse (detection) maps."""
    out: dict[int, int] = {}
    for s in range(0x80, 0x100):
        y = (s << 1) & 0xFF
        if y & 0x80:
            d = -((_lut(SLIDE_DEC_HI_BASE, y) << 8) | _lut(SLIDE_DEC_LO_BASE, y))
        else:
            d = (_lut(SLIDE_INC_HI_BASE, y) << 8) | _lut(SLIDE_INC_LO_BASE, y)
        out[s] = d
    return out


SLIDE_DELTAS = _build_slide_deltas()
# Inverse map: signed delta → slide_mode. Skip delta=0 (degenerate
# modes $80/$C0 produce no observable slide). Multiple modes can
# share a delta; we take the first.
SLIDE_MODE_FROM_DELTA: dict[int, int] = {}
for _s, _d in SLIDE_DELTAS.items():
    if _d != 0 and _d not in SLIDE_MODE_FROM_DELTA:
        SLIDE_MODE_FROM_DELTA[_d] = _s


def ps_depth_from_delta(delta: int) -> "int | None":
    """Reverse-map a per-frame 16-bit pulse delta to the ``PS`` depth
    byte that produces it. ``$81..$FF`` (ADD path): delta = magnitude
    = depth & $7F. ``$01..$7F`` (SUB path): delta = -(depth + 1),
    so depth = -delta - 1. Returns None for unrepresentable deltas
    (zero, ±1, |delta|>127 SUB or >127 ADD)."""
    if delta == 0:
        return None
    if 1 <= delta <= 0x7F:
        return 0x80 | delta
    if -128 <= delta <= -2:
        return -delta - 1
    return None


# ---------- LUT helpers ----------------------------------------------------


def build_freq_to_note_per_voice() -> list[dict[tuple[int, int], int]]:
    out: list[dict[tuple[int, int], int]] = []
    for pb in PITCH_BASE:
        m: dict[tuple[int, int], int] = {}
        for note in range(128):
            if NOTE_PITCH_LO[note] == 0 and NOTE_PITCH_HI[note] == 0:
                continue
            total_lo = NOTE_PITCH_LO[note] + pb
            carry = total_lo >> 8
            freq_lo = total_lo & 0xFF
            freq_hi = (NOTE_PITCH_HI[note] + carry) & 0xFF
            m.setdefault((freq_lo, freq_hi), note)
        out.append(m)
    return out


def snap_note(
    voice: int,
    freq_lo: int,
    freq_hi: int,
    freq_maps: list[dict[tuple[int, int], int]],
    prev_note: int,
) -> int:
    """Return note for an exact LUT match; if off-LUT, keep prev_note
    (the slide-driven freq trajectory is intentionally NOT recreated --
    the previous note's freq will continue to be emitted by the player,
    matching only the LUT-aligned freq writes in the input CSV)."""
    if (freq_lo, freq_hi) in freq_maps[voice]:
        return freq_maps[voice][(freq_lo, freq_hi)]
    return prev_note


# ---------- Stage A: replay CSV --------------------------------------------


@dataclass
class FrameState:
    note: list[int] = field(default_factory=lambda: [0, 0, 0])
    # Raw per-voice freq bytes, needed for slide detection. Note byte
    # snap drops off-LUT freqs; slide detection needs the original
    # trajectory to recover per-frame deltas.
    freq_lo: list[int] = field(default_factory=lambda: [0, 0, 0])
    freq_hi: list[int] = field(default_factory=lambda: [0, 0, 0])
    # Raw per-voice pulse bytes for PS detection (a constant-rate
    # 16-bit pulse sweep). ``pw_hi`` doubles as both the high byte of
    # PW (snippet column) and pulse_hi (PS state).
    pulse_lo: list[int] = field(default_factory=lambda: [0, 0, 0])
    pw_hi: list[int] = field(default_factory=lambda: [0, 0, 0])  # = pulse_hi
    ctrl: list[int] = field(default_factory=lambda: [0, 0, 0])
    ad: list[int] = field(default_factory=lambda: [0, 0, 0])
    sr: list[int] = field(default_factory=lambda: [0, 0, 0])
    fv: int = 0x0F  # player default at boot
    cp: int = 0x02
    re: int = 0


VOICE_FOR_REG = {}
for v in range(3):
    for f, rel in (
        ("freq_lo", 0),
        ("freq_hi", 1),
        ("pw_lo", 2),
        ("pw_hi", 3),
        ("ctrl", 4),
        ("ad", 5),
        ("sr", 6),
    ):
        VOICE_FOR_REG[7 * v + rel] = (v, f)


def replay_csv(csv_path: Path, freq_maps) -> list[FrameState]:
    raw_state = [0] * 25
    max_frame = -1
    rows_by_frame: dict[int, list[tuple[int, int]]] = defaultdict(list)
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            frame = int(row["frame"])
            reg = int(row["reg"])
            val = int(row["value"])
            rows_by_frame[frame].append((reg, val))
            max_frame = max(max_frame, frame)
    n_frames = max_frame + 1

    states: list[FrameState] = []
    cur = FrameState()
    for frame in range(n_frames):
        for reg, val in rows_by_frame.get(frame, []):
            raw_state[reg] = val
            if reg in VOICE_FOR_REG:
                v, f = VOICE_FOR_REG[reg]
                if f in ("freq_lo", "freq_hi"):
                    fl = raw_state[7 * v + 0]
                    fh = raw_state[7 * v + 1]
                    cur.freq_lo[v] = fl
                    cur.freq_hi[v] = fh
                    cur.note[v] = snap_note(v, fl, fh, freq_maps, cur.note[v])
                elif f in ("pw_lo", "pw_hi"):
                    cur.pulse_lo[v] = raw_state[7 * v + 2]
                    cur.pw_hi[v] = raw_state[7 * v + 3]
                elif f == "ctrl":
                    cur.ctrl[v] = val
                elif f == "ad":
                    cur.ad[v] = val
                elif f == "sr":
                    cur.sr[v] = val
            else:
                if reg == 22:
                    cur.cp = val
                elif reg == 23:
                    cur.re = val
                elif reg == 24:
                    cur.fv = val
        # snapshot a copy for this frame
        states.append(
            FrameState(
                note=list(cur.note),
                freq_lo=list(cur.freq_lo),
                freq_hi=list(cur.freq_hi),
                pulse_lo=list(cur.pulse_lo),
                pw_hi=list(cur.pw_hi),
                ctrl=list(cur.ctrl),
                ad=list(cur.ad),
                sr=list(cur.sr),
                fv=cur.fv,
                cp=cur.cp,
                re=cur.re,
            )
        )
    return states


# ---------- Stage B: phrase slicing per voice ------------------------------


@dataclass
class Phrase:
    voice: int
    start_frame: int
    length: int  # frames until next phrase (or song end)
    note: int  # snapped note byte at phrase start
    deltas: list[tuple[int, dict[str, int]]]
    # deltas[k] = (rel_frame, {column_name: byte_value}) for column
    # updates within the phrase. Column names: WGh, AD, SR, PW.


def gate_bit(ctrl: int) -> int:
    return ctrl & 0x01


def slice_voice_phrases(states: list[FrameState], voice: int) -> list[Phrase]:
    """Phrase boundary: note byte change OR gate-bit toggle. Each
    phrase captures (WGh, AD, SR, PW) at phrase start in a single
    sidTAB row. Slide continuations -- (gate-on, gate-off) phrase
    pairs whose combined freq trajectory fits an active slide -- are
    merged in a post-pass (``_merge_slide_continuations``)."""
    phrases: list[Phrase] = []
    cur_start = 0
    cur_note = states[0].note[voice]
    cur_gate = gate_bit(states[0].ctrl[voice])

    def flush(end: int):
        if end <= cur_start:
            return
        start = cur_start
        init_cols = {
            "WGh": states[start].ctrl[voice],
            "AD": states[start].ad[voice],
            "SR": states[start].sr[voice],
            "PW": states[start].pw_hi[voice],
        }
        phrases.append(
            Phrase(
                voice=voice,
                start_frame=start,
                length=end - start,
                note=states[start].note[voice],
                deltas=[(0, init_cols)],
            )
        )

    for f in range(1, len(states)):
        n = states[f].note[voice]
        g = gate_bit(states[f].ctrl[voice])
        if n != cur_note or g != cur_gate:
            flush(f)
            cur_start = f
            cur_note = n
            cur_gate = g
    flush(len(states))
    return phrases


def _detect_phrase_ps(phrase: Phrase, states: list[FrameState]) -> "int | None":
    """Detect a constant-rate PS sweep in ``phrase``.

    Walks per-frame 16-bit pulse (pw_hi << 8 | pulse_lo), computes
    deltas. If the first non-zero delta is followed by an unbroken
    linear progression to phrase end, match it against PS depth.

    Returns the ps_depth byte or None. The caller adds it as a
    ``"PS"`` column on the phrase's row 0 (PS engages at gate-on
    via the snippet's row 0 PS column write; the oscillator runs
    every frame thereafter)."""
    voice = phrase.voice
    if phrase.length < 3:
        return None
    pulse: list[int] = []
    for k in range(phrase.length):
        fs = states[phrase.start_frame + k]
        pulse.append((fs.pw_hi[voice] << 8) | fs.pulse_lo[voice])

    # Per-frame deltas (mod 16-bit, signed).
    deltas: list[int] = []
    for k in range(1, len(pulse)):
        d = (pulse[k] - pulse[k - 1]) & 0xFFFF
        if d >= 0x8000:
            d -= 0x10000
        deltas.append(d)
    if not deltas:
        return None

    # Find first non-zero delta. Everything before may be settling
    # (e.g. row apply hasn't fired yet).
    target_idx = None
    for k, d in enumerate(deltas):
        if d != 0:
            target_idx = k
            break
    if target_idx is None:
        return None

    target = deltas[target_idx]
    for j in range(target_idx + 1, len(deltas)):
        if deltas[j] != target:
            return None

    return ps_depth_from_delta(target)


def _merge_slide_continuations(
    voice_phrases: list[Phrase], states: list[FrameState]
) -> list[Phrase]:
    """Walk phrase pairs; merge (A, B) into a single phrase carrying
    the slide encoding when A is gate-on, B is gate-off, A.note ==
    B.note (snap_note keeps prev_note for B's off-LUT freqs), and
    the combined freq trajectory fits a constant-rate active slide.

    GATE_N (fired at every pattern-event trigger today) resets
    slide state, so an un-merged B would have its trigger wipe
    slide_mode set by A's snippet. Merging emits A's trigger only;
    A's multi-row snippet handles the slide engagement + gate-off
    WGh flip; the cascade reaches STop while B's frames play out
    with the slide accumulator still alive."""
    out: list[Phrase] = []
    i = 0
    while i < len(voice_phrases):
        p = voice_phrases[i]
        merged: "Phrase | None" = None
        if i + 1 < len(voice_phrases):
            nxt = voice_phrases[i + 1]
            if nxt.note == p.note:
                combined_length = nxt.start_frame + nxt.length - p.start_frame
                test_phrase = Phrase(
                    voice=p.voice,
                    start_frame=p.start_frame,
                    length=combined_length,
                    note=p.note,
                    deltas=[(0, dict(p.deltas[0][1]))],
                )
                result = _detect_phrase_slide(test_phrase, states)
                if result is not None:
                    t_internal, slide_mode, wgh_at_t_obs = result
                    if t_internal == 0:
                        test_phrase.deltas[0][1]["AF"] = slide_mode
                    else:
                        row1_cols: dict[str, int] = {"AF": slide_mode}
                        if wgh_at_t_obs != test_phrase.deltas[0][1]["WGh"]:
                            row1_cols["WGh"] = wgh_at_t_obs
                        test_phrase.deltas.append((t_internal, row1_cols))
                    merged = test_phrase
        if merged is not None:
            out.append(merged)
            i += 2
        else:
            out.append(p)
            i += 1
    return out


def _detect_phrase_slide(
    phrase: Phrase, states: list[FrameState]
) -> "tuple[int, int, int] | None":
    """Detect a constant-rate active slide in ``phrase``.

    Returns ``(t_internal, slide_mode, wgh_at_t_obs)`` on hit, else
    ``None``.

    ``t_internal`` is the rel_frame for the multi-row snippet's
    row-1 (where AF fires in the cascade). Per the player's emit-
    shift (foundational test ``TestMultiRowSlideEngagement``), the
    slide first becomes visible in observed_freq at
    ``t_obs = t_internal + 1``.

    Detection: walk per-frame raw freq vs ``NOTE_PITCH[note]`` (NOT
    the per-voice LUT-with-pitch_base — active slide bypasses
    pitch_base, per ``TestActiveSlideLinear``). Find first frame
    where drift != pitch_base (= ``t_obs``); compute the per-frame
    step from drift[t_obs+1] - drift[t_obs] mod 16-bit; verify
    linearity across all remaining frames; match step against
    ``SLIDE_MODE_FROM_DELTA``.

    ``wgh_at_t_obs`` lets the caller capture mid-phrase CTRL
    transitions (e.g. glow_worm's gate-off release slides) by
    including WGh in row 1 when it differs from row 0.
    """
    voice = phrase.voice
    pb = PITCH_BASE[voice]
    note = phrase.note
    base = (NOTE_PITCH_HI[note] << 8) | NOTE_PITCH_LO[note]
    if base == 0:  # silence note (note 0 has zero pitch LUT entry)
        return None

    drifts: list[int] = []
    for k in range(phrase.length):
        fs = states[phrase.start_frame + k]
        obs = (fs.freq_hi[voice] << 8) | fs.freq_lo[voice]
        d = (obs - base) & 0xFFFF
        if d >= 0x8000:
            d -= 0x10000
        drifts.append(d)

    # First frame where drift != pitch_base (= slide first observed).
    t_obs = None
    for k, d in enumerate(drifts):
        if d != pb:
            t_obs = k
            break
    if t_obs is None:
        return None
    # t_obs=0 would mean drift at phrase start != pitch_base, which
    # contradicts how slice_voice_phrases anchors phrase.note via
    # snap_note's on-LUT match. Skip defensively.
    # t_obs=len-1 leaves no frame for the linearity check.
    if t_obs == 0 or t_obs >= len(drifts) - 1:
        return None

    raw_step = drifts[t_obs + 1] - drifts[t_obs]
    step = ((raw_step + 0x8000) & 0xFFFF) - 0x8000
    if step == 0:
        return None

    # Linearity check across all off-LUT frames (mod 16-bit wrap).
    for j in range(t_obs + 1, len(drifts)):
        local_raw = drifts[j] - drifts[j - 1]
        local = ((local_raw + 0x8000) & 0xFFFF) - 0x8000
        if local != step:
            return None

    slide_mode = SLIDE_MODE_FROM_DELTA.get(step)
    if slide_mode is None:
        return None

    # T_internal = T_obs in glow_worm context. The relationship differs
    # from the foundational synthetic test because of frame anchoring:
    # synthetic-test trigger fires at frame 0 (= phrase frame -1
    # equivalent); glow_worm's chunked-pattern trigger fires at
    # ``start_frame - 1`` so phrase frame 0 emit captures end-of-
    # trigger-frame state. With DL_row0 = T_internal - 1 (lay_snippets
    # formula), row 1 fetches at phrase frame T_internal - 1, the
    # slide engages at end of that frame, and is visible at emit
    # phrase frame T_internal. Hence T_internal = T_obs.
    t_internal = t_obs
    wgh_at_t_obs = states[phrase.start_frame + t_obs].ctrl[voice]
    return t_internal, slide_mode, wgh_at_t_obs


# ---------- Stage B*: layer-1 cascade planning -----------------------------
#
# The defMON player walks a second per-voice cascade ("layer 1") driven by
# ``slot_b`` + ``FLAG_GATE_B`` pattern events (pydefmon/defmon_player.py
# line 978-980). Layer-1 carries global state side effects -- in glow_worm,
# the cutoff slide is an ACID column triplet (absolute set, SBC slide, ADC
# slide) walked via DL holds, gated via slot_b from V2's pattern body.
#
# Layer-1 design doc:
#   design/dump_to_defmon_layer1_design.md
#
# This stage adds:
#   * ``CutoffSlideState`` -- a faithful forward simulator of the player's
#     ``_cutoff_slide_step`` math (verified against the synthetic-prg tests
#     in ``tests/test_defmon_layer1_model.py``).
#   * ``apply_acid_column`` -- mutates the state per a 16-bit ACID column
#     value (matching the player's high-bitmap dispatch).
#   * ``Layer1Instrument`` -- frozen container for a layer-1 sidTAB row
#     sequence (rows + DLs); the layer-1 analog of ``Instrument``.
#   * ``profile_layer1_cutoff`` -- scans the source CSV's cutoff trajectory,
#     identifies "epoch boundaries" (reset frames where the trajectory
#     jumps to the cycle peak), and returns the planned layer-1 events.
#
# Step 2 of the rollout: infrastructure only. ``build_song`` does not yet
# consume the planner output. Step 3 wires it.


@dataclass
class CutoffSlideState:
    """Mirror of the player's cutoff_slide accumulator + step + flags.

    Forward simulator: ``step()`` advances one frame and returns the
    emitted $D416 byte; ``apply_acid_column()`` mutates state per a
    sidTAB row's ACID column value.

    Field semantics match the player one-for-one:
      acc_lo, acc_hi   -- $10B6 / $10BE
      step_lo, step_hi -- $10B9 / $10C0
      op_is_adc        -- $10B8 / $10BF opcode patch ($69 ADC / $E9 SBC)
      extra            -- $10CA (CP column store)
      floor            -- $10CE (default $02 per startup-RNG branch)
      asl              -- $10D4 = NOP/ASL (default NOP per startup-RNG)
    """

    acc_lo: int = 0
    acc_hi: int = 0
    step_lo: int = 0
    step_hi: int = 0
    op_is_adc: bool = True
    extra: int = 0
    floor: int = 0x02
    asl: bool = False

    def copy(self) -> "CutoffSlideState":
        return CutoffSlideState(
            self.acc_lo,
            self.acc_hi,
            self.step_lo,
            self.step_hi,
            self.op_is_adc,
            self.extra,
            self.floor,
            self.asl,
        )

    def step(self) -> int:
        """Advance one frame; return the emitted $D416 byte. Mirrors
        ``DefmonPlayer._cutoff_slide_step``."""
        carry = 0  # CLC at $10B7
        if self.op_is_adc:
            raw = self.acc_lo + self.step_lo + carry
            new_lo = raw & 0xFF
            carry = 1 if raw > 0xFF else 0
        else:
            raw = self.acc_lo - self.step_lo - (1 - carry)
            new_lo = raw & 0xFF
            carry = 1 if raw >= 0 else 0
        self.acc_lo = new_lo
        if self.op_is_adc:
            raw = self.acc_hi + self.step_hi + carry
            new_hi = raw & 0xFF
            hc = 1 if raw > 0xFF else 0
        else:
            raw = self.acc_hi - self.step_hi - (1 - carry)
            new_hi = raw & 0xFF
            hc = 1 if raw >= 0 else 0
        if new_hi & 0x80:
            new_hi = self.floor
        self.acc_hi = new_hi
        raw = new_hi + self.extra + hc
        a = raw & 0xFF
        if (a & 0x80) or a < 2:
            a = self.floor
        if self.asl:
            a = (a << 1) & 0xFF
        return a


def apply_acid_column(state: CutoffSlideState, val: int) -> None:
    """Apply a sidTAB row's ACID column (16-bit) to ``state``. Mirrors
    ``DefmonPlayer._apply_high_bitmap_globals`` ACID branch.

    Byte order (per player line 1232):
      val = (raw[pos] << 8) | raw[pos+1]
      low  = (val >> 8) & 0xFF   -- upper byte of val
      high = val & 0xFF          -- lower byte of val
    """
    low = (val >> 8) & 0xFF
    high = val & 0xFF
    if not (high & 0x80):
        # Absolute set: acc := (low, high), step := 0.
        state.acc_lo = low
        state.acc_hi = high
        state.step_lo = 0
        state.step_hi = 0
        return
    # Slide command: step_lo := low; step_hi := high & $3F (SBC) or
    # $7F (ADC); op_is_adc := !(high & $40).
    state.step_lo = low
    if high & 0x40:
        state.step_hi = high & 0x3F
        state.op_is_adc = False
    else:
        state.step_hi = high & 0x7F
        state.op_is_adc = True


@dataclass(frozen=True)
class Layer1Instrument:
    """A layer-1 sidTAB row sequence: ordered ``(cols, dl)`` entries.

    Each tuple in ``rows`` is the sorted column dict for one sidTAB
    row; ``dls`` carries the per-row DL byte. The cascade walks
    rows[0] -> rows[1] -> ... via DL hold, with the final row's
    ``dl=$80`` STop freezing the cascade.
    """

    rows: tuple[tuple[tuple[str, int], ...], ...]
    dls: tuple[int, ...]

    @classmethod
    def acid_drive_3row(cls) -> "Layer1Instrument":
        """glow_worm's row-44/45/46 shape: ACID abs $0040, SBC step=2
        slide held 19 frames, ADC step=2 slide held 19 frames then
        STop. Produces a 38-frame down-then-up cutoff cycle from a
        single GATE_B re-arm."""
        return cls(
            rows=(
                (("ACID", 0x0040),),
                (("ACID", 0x00C2),),
                (("ACID", 0x0082),),
            ),
            dls=(0x00, 0x12, 0x80),
        )


def profile_layer1_cutoff(states: list["FrameState"]) -> list[int]:
    """Scan ``states[].cp`` (the running $D416 emit) for reset
    frames: frames F where cutoff = $40 AND cutoff[F] - cutoff[F-1]
    > 1, signalling that an ACID absolute-set was applied during
    frame F-1's cascade tick.

    Returns the list of GATE_B fire frames: ``[F - 1, ...]``. Each
    entry is the frame at which a layer-0 pattern event needs to
    fire ``FLAG_GATE_B`` with ``slot_b`` pointing at the ACID-drive
    layer-1 instrument's start row.

    Per-cycle expected gain: ~38 frames of cutoff trajectory match
    after each reset (down 19 frames + up 19 frames before the
    layer-1 cascade reaches the STop row).
    """
    if not states:
        return []
    fires: list[int] = []
    for f in range(1, len(states)):
        cur = states[f].cp
        prev = states[f - 1].cp
        if cur == 0x40 and abs(cur - prev) > 1:
            fires.append(f - 1)
    return fires


# ---------- Stage C: cluster phrases -> instruments ------------------------


@dataclass(frozen=True)
class Instrument:
    """A sidTAB snippet shape: ordered (rel_frame, columns) entries."""

    deltas: tuple[tuple[int, tuple[tuple[str, int], ...]], ...]
    length: int

    @classmethod
    def from_phrase(cls, p: Phrase) -> "Instrument":
        norm = tuple((rel, tuple(sorted(cols.items()))) for rel, cols in p.deltas)
        return cls(deltas=norm, length=p.length)


def cluster_phrases(phrases: list[Phrase]) -> tuple[dict[Instrument, int], list[int]]:
    """Returns (instruments, phrase_instrument_idx_list)."""
    insts: dict[Instrument, int] = {}
    out: list[int] = []
    for p in phrases:
        inst = Instrument.from_phrase(p)
        if inst not in insts:
            insts[inst] = len(insts)
        out.append(insts[inst])
    return insts, out


# ---------- Stage D: lay snippets in sidTAB --------------------------------


@dataclass
class Snippet:
    start_row: int
    n_rows: int


def lay_snippets(instruments: dict[Instrument, int], next_row: int) -> tuple[
    dict[int, Snippet],
    dict[int, bytes],
    dict[int, int],  # row -> DL byte
    dict[int, int],
]:  # row -> JP target (0 = active linear)
    """Allocate sidTAB rows for each instrument. Returns:
    inst_idx -> Snippet
    row -> 15-byte SidtabRow packed bytes
    row -> DL byte (0..127 hold; 128+ STop)
    row -> JP target (-1 = active-linear/JP=$11, else jump-target row)
    """
    snippets: dict[int, Snippet] = {}
    row_bytes: dict[int, bytes] = {}
    row_dl: dict[int, int] = {}
    row_jp: dict[int, int] = {}

    for inst, idx in instruments.items():
        deltas = inst.deltas
        n_rows = len(deltas)
        snippets[idx] = Snippet(start_row=next_row, n_rows=n_rows)
        for i, (rel, cols) in enumerate(deltas):
            row = next_row + i
            row_bytes[row] = SidtabRow.pack(dict(cols))
            row_jp[row] = -1  # active-linear by default
            if i + 1 < n_rows:
                next_rel = deltas[i + 1][0]
                gap = next_rel - rel
                row_dl[row] = min(0x7F, max(0, gap - 1))
            else:
                # Final row: apply columns then STop immediately so the
                # cascade freezes (next pattern event re-gates a new
                # snippet). Player keeps emitting the frozen patch slot
                # values per frame.
                row_dl[row] = 0x80
        next_row += n_rows

    return snippets, row_bytes, row_dl, row_jp


# ---------- Stage F-G: patterns + arranger ---------------------------------


CHUNK_FRAMES = 63  # frames per song step; with ALT(dur=0) gives exactly
# 63 frames per pattern when filled with 31 dur=0 events
# (31*2 + 1 ALT frame = 63).


def _delay_event(frames: int) -> PatternEvent:
    """Pure-delay PatternEvent that takes (frames) total frames before
    next fetch. Returns None if frames < 2 (cannot encode)."""
    if frames < 2:
        raise ValueError(f"delay event needs >= 2 frames, got {frames}")
    dur = min(15, frames - 2)
    return PatternEvent.delay(dur)


def _build_voice_chunks(
    phrases: list[Phrase],
    phrase_inst_idx: list[int],
    snippets_for_voice: dict[int, Snippet],
    n_chunks: int,
    gate_b_attachments: dict[int, int] | None = None,
) -> list[list[PatternEvent]]:
    """Per-voice: return n_chunks pattern bodies (each ending in ALT)
    such that pattern Y covers frames [Y*CHUNK_FRAMES, (Y+1)*CHUNK_FRAMES).
    Triggers fire at the right rel_frame; intra-chunk slots filled with
    delays; pattern ends with ALT(dur=0) so all voices' patterns
    consume exactly CHUNK_FRAMES.

    ``gate_b_attachments``: ``{trigger_frame: layer1_start_row}``. When
    a phrase's trigger fires at one of these frames, the emitted
    PatternEvent additionally sets ``FLAG_GATE_B`` + ``slot_b`` so the
    player re-arms layer 1 alongside layer 0.
    """
    gate_b_attachments = gate_b_attachments or {}
    # Partition phrases by their TRIGGER frame, which is one frame
    # BEFORE the phrase's start_frame (the player's pattern event sets
    # voice state during frame F, so the change is visible on the
    # next frame's emit; emit at frame F+1 ~= CSV row at frame F+1).
    # Phrases at start_frame=0 require no trigger (their state matches
    # the player's initial state).
    chunk_phrases: list[list[tuple[int, int, int, int]]] = [[] for _ in range(n_chunks)]
    for phrase, inst_idx in zip(phrases, phrase_inst_idx):
        if phrase.start_frame == 0:
            continue
        trigger_frame = phrase.start_frame - 1
        ch = trigger_frame // CHUNK_FRAMES
        if ch >= n_chunks:
            continue
        rel = trigger_frame % CHUNK_FRAMES
        snip = snippets_for_voice[inst_idx]
        slot_b = gate_b_attachments.get(trigger_frame, 0)
        chunk_phrases[ch].append((rel, snip.start_row, phrase.note, slot_b))

    bodies: list[list[PatternEvent]] = []
    for triples in chunk_phrases:
        triples.sort(key=lambda t: t[0])
        events: list[PatternEvent] = []
        cur_rel = 0
        target_rel = CHUNK_FRAMES - 1  # leave 1 frame for ALT
        # Drop triggers that won't fit in 31 events.
        max_trigger_events = 31
        # Pre-compute the duration nibble for each trigger event.
        # A trigger with dur=N consumes N+2 frames. The default is dur=0
        # (2 frames), with the remaining gap to the next trigger filled
        # by pure-delay events (min 2 frames). When the next trigger sits
        # exactly 3 rels away the delay can't bridge a 1-frame gap, so
        # bump the prior trigger to dur=1 to land cur_rel exactly on the
        # next rel.
        durs = []
        for i, (rel, _, _, _) in enumerate(triples):
            next_rel = triples[i + 1][0] if i + 1 < len(triples) else None
            durs.append(1 if next_rel is not None and next_rel - rel == 3 else 0)
        for i, (rel, snip_row, note, slot_b) in enumerate(triples):
            # Insert delays to advance cur_rel to rel.
            while cur_rel + 2 <= rel and len(events) < max_trigger_events - 1:
                gap = rel - cur_rel
                events.append(_delay_event(gap))
                cur_rel += events[-1].duration + 2
            if cur_rel > rel:
                # Overshot: drop this phrase (would fire too late).
                continue
            if cur_rel < rel:
                # 1-frame underfill (gap=1 to prior trigger, or rel=1
                # from chunk start). Tried emitting 1 frame early --
                # net loss vs dropping (the shift broke alignment for
                # too many CSV columns). Drop the phrase.
                continue
            if len(events) >= max_trigger_events:
                break
            dur = durs[i]
            flag = PatternEvent.FLAG_GATE_N | PatternEvent.FLAG_GATE_A | dur
            if slot_b != 0:
                flag |= PatternEvent.FLAG_GATE_B
            events.append(
                PatternEvent(
                    flag=flag,
                    slot_a=snip_row & 0xFF,
                    slot_b=slot_b & 0xFF,
                    note=note & 0x7F,
                )
            )
            cur_rel += dur + 2
        # Fill remaining with delays until cur_rel == target_rel.
        while cur_rel + 2 <= target_rel and len(events) < 31:
            gap = target_rel - cur_rel
            events.append(_delay_event(gap))
            cur_rel += events[-1].duration + 2
        # Drop ALT_dur if exact fit; else use ALT.dur to absorb 0-1 frame slack.
        slack = target_rel - cur_rel  # 0 or 1 expected
        alt_dur = max(0, slack)
        events.append(PatternEvent.alt_end(alt_dur))
        # Pad to exactly 32 events. ALT halts pattern advance so trailing
        # dummy events are never reached.
        while len(events) < 32:
            events.append(PatternEvent.delay(0))
        bodies.append(events)
    return bodies


def build_chunked_patterns(
    voice_phrases,
    voice_inst_idx,
    snippets,
    total_frames: int,
    gate_b_per_voice: dict[int, dict[int, int]] | None = None,
):
    """``gate_b_per_voice``: per-voice ``{trigger_frame:
    layer1_start_row}`` attachments emitted alongside layer-0
    triggers."""
    gate_b_per_voice = gate_b_per_voice or {}
    n_chunks = (total_frames + CHUNK_FRAMES - 1) // CHUNK_FRAMES
    out = []
    for v in range(3):
        snips_v = {idx: snippets[idx] for idx in set(voice_inst_idx[v])}
        out.append(
            _build_voice_chunks(
                voice_phrases[v],
                voice_inst_idx[v],
                snips_v,
                n_chunks,
                gate_b_attachments=gate_b_per_voice.get(v),
            )
        )
    return out, n_chunks


# ---------- main pipeline --------------------------------------------------


def build_song(csv_path: Path, verbose: bool = True) -> DefmonSong:
    freq_maps = build_freq_to_note_per_voice()
    states = replay_csv(csv_path, freq_maps)
    if verbose:
        print(f"loaded {len(states)} frames from {csv_path}", file=sys.stderr)

    # Stage B
    voice_phrases = [slice_voice_phrases(states, v) for v in range(3)]
    for v in range(3):
        print(f"V{v}: {len(voice_phrases[v])} phrases", file=sys.stderr)

    # Stage B': merge slide continuations. Gate-on + gate-off pairs
    # with matching note (= prev_note carried through B's off-LUT
    # frames) whose combined trajectory fits a constant-rate slide
    # become one multi-row phrase. The B phrase's pattern trigger
    # is dropped, so GATE_N can't wipe the slide_mode that A's
    # row 1 set.
    pre_counts = [len(voice_phrases[v]) for v in range(3)]
    for v in range(3):
        voice_phrases[v] = _merge_slide_continuations(voice_phrases[v], states)
    n_merged = sum(pre - len(voice_phrases[v]) for v, pre in enumerate(pre_counts))
    print(f"slide continuations merged: {n_merged}", file=sys.stderr)

    # Stage B'': PS detection. For each phrase with a constant-rate
    # 16-bit pulse-width sweep, add a ``PS`` column to row 0. PS
    # depth is NOT reset by GATE_N (only slide state is), so the
    # snippet's row-0 PS column is the natural place to engage the
    # sweep at the gate-on frame.
    n_ps_hits = 0
    for v in range(3):
        for p in voice_phrases[v]:
            ps_depth = _detect_phrase_ps(p, states)
            if ps_depth is not None:
                p.deltas[0][1]["PS"] = ps_depth
                n_ps_hits += 1
    print(f"PS hits: {n_ps_hits}", file=sys.stderr)

    # Stage C: cluster phrases into a single global instrument table.
    # Same (WGh, AD, SR, PW) cluster on V0/V1/V2 shares one snippet --
    # the player's sidTAB is global, so per-voice partitioning just
    # wastes rows on duplicates.
    global_inst_map: dict[Instrument, int] = {}
    voice_phrase_idx: list[list[int]] = [[], [], []]
    for v in range(3):
        for p in voice_phrases[v]:
            inst = Instrument.from_phrase(p)
            if inst not in global_inst_map:
                global_inst_map[inst] = len(global_inst_map)
            voice_phrase_idx[v].append(global_inst_map[inst])
    print(
        f"global: {len(global_inst_map)} unique instruments",
        file=sys.stderr,
    )

    # Stage D: lay each instrument once in the shared sidTAB.
    snippets, row_bytes, row_dl, row_jp = lay_snippets(global_inst_map, next_row=1)
    next_row = max(row_bytes) + 1 if row_bytes else 1
    if next_row > 256:
        print(
            f"WARN: blew sidTAB capacity (used row {next_row-1})",
            file=sys.stderr,
        )

    # Stage D': lay the layer-1 ACID drive (a 4-row JP-loop walk:
    # abs $0040 / SBC step=2 / ADC step=2 / JP-back). Pack the rows
    # immediately after layer-0 so row indices stay 1-byte.
    layer1_start_row = next_row
    layer1_abs_row = next_row
    layer1_sbc_row = next_row + 1
    layer1_adc_row = next_row + 2
    layer1_jp_marker_row = next_row + 3
    row_bytes[layer1_abs_row] = SidtabRow.pack({"ACID": 0x0040})
    row_dl[layer1_abs_row] = 0x00  # advance after 1 frame
    row_jp[layer1_abs_row] = -1  # active linear
    row_bytes[layer1_sbc_row] = SidtabRow.pack({"ACID": 0x00C2})
    row_dl[layer1_sbc_row] = 0x12  # hold 19 frames
    row_jp[layer1_sbc_row] = -1
    row_bytes[layer1_adc_row] = SidtabRow.pack({"ACID": 0x0082})
    row_dl[layer1_adc_row] = 0x12
    row_jp[layer1_adc_row] = -1
    # JP marker: empty row, JP redirect back to the SBC row so the
    # cascade walks SBC -> ADC -> (jp) -> SBC -> ADC forever.
    row_bytes[layer1_jp_marker_row] = SidtabRow.pack({})
    row_dl[layer1_jp_marker_row] = 0x00
    row_jp[layer1_jp_marker_row] = layer1_sbc_row
    next_row += 4

    print(f"used {next_row}/256 sidTAB rows", file=sys.stderr)
    print(
        f"layer-1 ACID drive at rows {layer1_abs_row}..{layer1_jp_marker_row}",
        file=sys.stderr,
    )

    # Stage E': profile the cutoff trajectory; attach layer-1
    # engagement (FLAG_GATE_B + slot_b) to the closest layer-0
    # trigger at each fire frame. The layer-1 cascade then walks
    # the loop on its own; subsequent trigger frames stay layer-0.
    gate_b_fires = profile_layer1_cutoff(states)
    voice_trigger_frames: dict[int, set[int]] = {v: set() for v in range(3)}
    for v in range(3):
        for p in voice_phrases[v]:
            if p.start_frame > 0:
                voice_trigger_frames[v].add(p.start_frame - 1)
    gate_b_per_voice: dict[int, dict[int, int]] = {v: {} for v in range(3)}
    n_attached = 0
    for fire in gate_b_fires:
        # Find a voice whose layer-0 trigger lands at this fire frame
        # (preferring V2, then V1, then V0).
        for v in (2, 1, 0):
            if fire in voice_trigger_frames[v]:
                # First-wins: don't overwrite an existing attachment.
                if fire not in gate_b_per_voice[v]:
                    gate_b_per_voice[v][fire] = layer1_start_row
                    n_attached += 1
                break
    print(
        f"layer-1 GATE_B: {n_attached}/{len(gate_b_fires)} fires attached",
        file=sys.stderr,
    )

    # Stage F: chunked patterns per voice (all voices share song steps).
    total_frames = len(states)
    voice_bodies, n_chunks = build_chunked_patterns(
        voice_phrases,
        voice_phrase_idx,
        snippets,
        total_frames,
        gate_b_per_voice=gate_b_per_voice,
    )
    for v in range(3):
        max_events = max((len(b) for b in voice_bodies[v]), default=0)
        print(
            f"V{v}: {len(voice_bodies[v])} patterns, max events/pattern = {max_events}",
            file=sys.stderr,
        )
    print(f"total chunks (= song steps): {n_chunks}", file=sys.stderr)

    # Stage G: assemble DefmonSong
    song = DefmonSong()
    # Silent pattern 0 for unused voice slots.
    song.set_pattern_events(0, PatternEvent.silent_pattern())
    # Allocate pattern indices, deduping identical event sequences
    # across voices. After cross-voice instrument merge, two chunks
    # whose triggers all share (rel, snip_row, note) produce the
    # same body and can share a pattern slot. Empty-trigger chunks
    # collapse to a single shared "rest" pattern.
    pat_idx = 1
    pattern_alloc: list[list[int]] = [[], [], []]
    body_to_idx: dict[tuple[bytes, ...], int] = {}
    for v in range(3):
        for body in voice_bodies[v]:
            key = tuple(ev.to_bytes() for ev in body)
            cached = body_to_idx.get(key)
            if cached is not None:
                pattern_alloc[v].append(cached)
                continue
            if pat_idx >= 128:
                print(
                    f"WARN: pattern overflow; V{v} chunk skipped at pat_idx {pat_idx}",
                    file=sys.stderr,
                )
                pattern_alloc[v].append(0)
                continue
            song.set_pattern_events(pat_idx, body)
            body_to_idx[key] = pat_idx
            pattern_alloc[v].append(pat_idx)
            pat_idx += 1

    # Arranger: song step Y plays (V0=pattern_alloc[0][Y], V1=..., V2=...).
    song.clear_song_table()
    for y in range(n_chunks):
        v1 = pattern_alloc[0][y] if y < len(pattern_alloc[0]) else 0
        v2 = pattern_alloc[1][y] if y < len(pattern_alloc[1]) else 0
        v3 = pattern_alloc[2][y] if y < len(pattern_alloc[2]) else 0
        song.set_step(y, v1=v1, v2=v2, v3=v3)
    # Loop back to step 0 at the end.
    song.set_jump(n_chunks, target=0, count=0)

    # Write sidTAB rows + JP/DL bytes.
    for row, raw in row_bytes.items():
        if row >= 256:
            continue
        off = 0x5F00 - LOAD_ADDRESS + row * 15
        for i, b in enumerate(raw):
            song.snapshot[off + i] = b
    for row, dl in row_dl.items():
        if row >= 256:
            continue
        song.set_dl(row, dl & 0xFF)
    for row, jp_target in row_jp.items():
        if row >= 256:
            continue
        if jp_target < 0:
            song.set_jp(row)  # active-linear ($11)
        else:
            song.set_jp(row, target=jp_target)

    # IRQ rate: 23546 cycles per frame (~41.84 Hz PAL).
    song.snapshot[0x715A - LOAD_ADDRESS] = DEFAULT_CYCLES_PER_FRAME & 0xFF
    song.snapshot[0x715B - LOAD_ADDRESS] = (DEFAULT_CYCLES_PER_FRAME >> 8) & 0xFF
    song.snapshot[0x715C - LOAD_ADDRESS] = 1  # single sub-frame
    return song


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv", type=Path)
    ap.add_argument("prg", type=Path)
    args = ap.parse_args(argv)

    song = build_song(args.csv)
    song.to_file(str(args.prg))
    print(f"wrote {args.prg}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
