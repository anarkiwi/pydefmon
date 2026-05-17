"""Foundational tests for defMON's pitch-slide oscillator.

Builds minimal synthetic .prgs that exercise specific slide behaviours,
runs them through DefmonPlayer, and asserts observed per-frame freq
trajectories match LUT-derived predictions. These tests validate the
model ``dump_to_defmon``'s slide reverse-mapping will rely on; if any
assertion here fails, the reverse-mapping algorithm needs revision
before the slides design can land.

Player references:
  pydefmon/defmon_player.py:700   _pitch_slide_voice
  pydefmon/defmon_player.py:1018  _sidcall_tick (DL semantics)
  pydefmon/defmon_player.py:981   GATE_N -> slide state reset
  pydefmon/defmon_player.py:488   _emit_frame_writes (emit-shift)

DefmonSong gotcha: ``clear_song_table()`` wipes $1B00..$1EFF (the
``voice_pattern_refs`` property's full 0x400-byte range), which
INCLUDES the DL region at $1E00..$1EFF. ``_build_minimal_song`` below
calls ``clear_song_table()`` BEFORE ``set_dl``. ``dump_to_defmon.build_song``
follows the same order. Reversing the order silently zeroes every DL
byte; the cascade then refetches every frame instead of holding rows.
"""

import unittest

from pydefmon.defmon import (
    LOAD_ADDRESS,
    DefmonSong,
    PatternEvent,
    SidtabRow,
)
from pydefmon.defmon_player import (
    DefmonPlayer,
    NOTE_PITCH_HI,
    NOTE_PITCH_LO,
    SID_REG_BASE,
    SLIDE_DEC_HI_BASE,
    SLIDE_DEC_LO_BASE,
    SLIDE_INC_HI_BASE,
    SLIDE_INC_LO_BASE,
    _lut,
)

PAL_CYCLES_PER_FRAME = 23546


def _s16(x: int) -> int:
    """Unsigned 16-bit → signed."""
    x &= 0xFFFF
    return x - 0x10000 if x >= 0x8000 else x


def predicted_slide_delta(slide_mode: int) -> int:
    """Predicted signed 16-bit per-frame accumulator delta for an
    active slide mode in ``$80..$FF``. ``$80`` and ``$C0`` are boundary
    modes with delta = 0."""
    y = (slide_mode << 1) & 0xFF
    if y & 0x80:
        return -((_lut(SLIDE_DEC_HI_BASE, y) << 8) | _lut(SLIDE_DEC_LO_BASE, y))
    return (_lut(SLIDE_INC_HI_BASE, y) << 8) | _lut(SLIDE_INC_LO_BASE, y)


def _base_freq(note: int) -> int:
    return (NOTE_PITCH_HI[note] << 8) | NOTE_PITCH_LO[note]


def _write_sidtab_row(song: DefmonSong, row: int, cols: dict, dl: int) -> None:
    """Pack a sidTAB row's columns + write the row body, DL, JP into
    the snapshot at the canonical addresses."""
    raw = SidtabRow.pack(cols)
    off = 0x5F00 - LOAD_ADDRESS + row * 15
    for i, b in enumerate(raw):
        song.snapshot[off + i] = b
    song.set_dl(row, dl & 0xFF)
    song.set_jp(row)  # active-linear


def _build_minimal_song(
    voice: int,
    rows: list[tuple[dict, int]],
    pattern_events: list[PatternEvent],
) -> DefmonSong:
    """Build a one-voice .prg that runs the supplied pattern on
    ``voice``, with the supplied sidTAB rows starting at row 1.

    Args:
        voice: 0/1/2.
        rows: list of (cols, dl) pairs; row index = position + 1.
        pattern_events: 32 PatternEvents for pattern 1.

    Returns:
        A DefmonSong ready for DefmonPlayer. clear_song_table is
        called BEFORE set_dl so DL writes survive.
    """
    song = DefmonSong()
    song.set_pattern_events(0, PatternEvent.silent_pattern())
    song.set_pattern_events(1, pattern_events)

    # ORDER CRITICAL: clear_song_table wipes $1B00..$1EFF; must run
    # BEFORE set_dl (which writes into $1E00).
    song.clear_song_table()
    voice_patterns = [0, 0, 0]
    voice_patterns[voice] = 1
    song.set_step(0, v1=voice_patterns[0], v2=voice_patterns[1], v3=voice_patterns[2])
    song.set_jump(1, target=0, count=0)

    for idx, (cols, dl) in enumerate(rows, start=1):
        _write_sidtab_row(song, idx, cols, dl)

    song.snapshot[0x715A - LOAD_ADDRESS] = PAL_CYCLES_PER_FRAME & 0xFF
    song.snapshot[0x715B - LOAD_ADDRESS] = (PAL_CYCLES_PER_FRAME >> 8) & 0xFF
    song.snapshot[0x715C - LOAD_ADDRESS] = 1  # one sub-frame per main frame
    return song


def _gate_on_pattern(note: int, snippet_row: int) -> list[PatternEvent]:
    """Pattern with a single gate-on event at slot 0 + long delay tail."""
    pat = [
        PatternEvent(
            flag=PatternEvent.FLAG_GATE_N | PatternEvent.FLAG_GATE_A,
            slot_a=snippet_row,
            slot_b=0,
            note=note & 0x7F,
        )
    ]
    for _ in range(30):
        pat.append(PatternEvent.delay(15))
    pat.append(PatternEvent.alt_end(15))
    return pat


def _render_voice_freq(
    song: DefmonSong, voice: int, n_frames: int
) -> list[tuple[int, int]]:
    """Run the player for n_frames, return per-frame emit-time
    (freq_lo, freq_hi) on ``voice``. The player's emit-shift model
    means observed[0] captures the pre-init state (zeros); the first
    cascade-applied value appears in observed[1]."""
    player = DefmonPlayer(song)
    fl_reg = SID_REG_BASE + voice * 7 + 0
    fh_reg = SID_REG_BASE + voice * 7 + 1
    fl, fh = 0, 0
    out: list[tuple[int, int]] = []
    for _ in range(n_frames):
        for reg, val in player.play_frame():
            if reg == fl_reg:
                fl = val & 0xFF
            elif reg == fh_reg:
                fh = val & 0xFF
        out.append((fl, fh))
    return out


def _render_voice_pw(
    song: DefmonSong, voice: int, n_frames: int
) -> list[tuple[int, int]]:
    """Per-frame emit-time (pulse_lo, pulse_hi) on ``voice``."""
    player = DefmonPlayer(song)
    pl_reg = SID_REG_BASE + voice * 7 + 2
    ph_reg = SID_REG_BASE + voice * 7 + 3
    pl, ph = 0, 0
    out: list[tuple[int, int]] = []
    for _ in range(n_frames):
        for reg, val in player.play_frame():
            if reg == pl_reg:
                pl = val & 0xFF
            elif reg == ph_reg:
                ph = val & 0xFF
        out.append((pl, ph))
    return out


def predicted_ps_delta(ps_depth: int) -> int:
    """Predicted per-frame 16-bit pulse delta for a PS depth byte.
    Returns 0 for no-PS modes ($00, $80). Inverse map below."""
    d = ps_depth & 0xFF
    if d == 0 or d == 0x80:
        return 0
    if d & 0x80:
        return d & 0x7F  # ADD path: magnitude
    return -(d + 1)  # SUB path: C-in=0 means decrement is depth+1


def ps_depth_from_delta(delta: int) -> "int | None":
    """Inverse: signed per-frame delta -> ps_depth byte that produces
    it. Returns None for unrepresentable deltas (-1, 0, and outside
    [-128, +127] with the SUB +1 offset)."""
    if delta == 0:
        return None  # caller treats as "no PS column"
    if delta > 0:
        if 1 <= delta <= 0x7F:
            return 0x80 | delta
        return None
    # delta < 0: SUB. depth = -delta - 1. depth in $01..$7F means
    # delta in -2..-128.
    if -128 <= delta <= -2:
        return -delta - 1
    return None


class TestNoSlideBaseline(unittest.TestCase):
    """No-slide path: freq = NOTE_PITCH[note] + pitch_base.
    pitch_base = 0/1/2 for V0/V1/V2 in the static binary
    (pydefmon/defmon_player.py:357)."""

    def test_no_slide_all_voices(self):
        note = 60
        cols = {"WGh": 0x11, "TR": 0x80 | note, "AD": 0x09, "SR": 0xA0}
        for voice, pb in enumerate((0, 1, 2)):
            with self.subTest(voice=voice):
                song = _build_minimal_song(
                    voice, [(cols, 0x80)], _gate_on_pattern(note, 1)
                )
                freqs = _render_voice_freq(song, voice, n_frames=8)
                expected_lo = (NOTE_PITCH_LO[note] + pb) & 0xFF
                expected_hi = (
                    NOTE_PITCH_HI[note] + ((NOTE_PITCH_LO[note] + pb) >> 8)
                ) & 0xFF
                # Frame 0 emits the pre-init state (zeros).
                self.assertEqual(freqs[0], (0, 0))
                for k, (fl, fh) in enumerate(freqs[1:], start=1):
                    self.assertEqual(
                        (fl, fh),
                        (expected_lo, expected_hi),
                        f"V{voice} frame {k}: ({fl:#x},{fh:#x}) != "
                        f"NOTE_PITCH[{note}]+pb"
                        f"=({expected_lo:#x},{expected_hi:#x})",
                    )


class TestActiveSlideLinear(unittest.TestCase):
    """Active slide modes ($80..$FF) integrate accumulator by a fixed
    LUT-derived delta per frame. ``observed[k] - NOTE_PITCH[note] ==
    k * predicted_delta(S)`` for k >= 1. The active-slide path
    emits freq WITHOUT pitch_base offset (player line 796-801) — all
    three voices show identical drift."""

    # Sample of modes covering both directions + the full magnitude
    # range. $80 and $C0 are boundary zero-delta modes; verified
    # separately to anchor the LUT structure.
    NONZERO_ACTIVE_MODES = (
        0x88,
        0x90,
        0xA0,
        0xB0,
        0xBF,
        0xC8,
        0xD0,
        0xE0,
        0xF0,
        0xFF,
    )

    def test_active_slide_linear_progression(self):
        note = 60
        n_frames = 10
        for slide_mode in self.NONZERO_ACTIVE_MODES:
            for voice in (0, 1, 2):
                with self.subTest(slide_mode=slide_mode, voice=voice):
                    cols = {
                        "WGh": 0x11,
                        "TR": 0x80 | note,
                        "AD": 0x09,
                        "SR": 0xA0,
                        "AF": slide_mode,
                    }
                    song = _build_minimal_song(
                        voice, [(cols, 0x80)], _gate_on_pattern(note, 1)
                    )
                    freqs = _render_voice_freq(song, voice, n_frames)
                    base = _base_freq(note)
                    delta = predicted_slide_delta(slide_mode)
                    # Compare modulo 65536 — the player's 16-bit accum
                    # wraps for large-magnitude modes (e.g. S=$BF delta
                    # +12601 wraps after frame 3).
                    for k, (fl, fh) in enumerate(freqs[1:], start=1):
                        obs = (fh << 8) | fl
                        actual = (obs - base) & 0xFFFF
                        expected = (k * delta) & 0xFFFF
                        self.assertEqual(
                            actual,
                            expected,
                            f"V{voice} S={slide_mode:#x} frame {k}: "
                            f"(obs-base) mod 65536 = {actual} != "
                            f"({k}*{delta}) mod 65536 = {expected}",
                        )

    def test_zero_rate_boundary_modes(self):
        """$80 and $C0 are boundary modes with predicted_delta = 0
        (per the LUT structure: SLIDE_INC[0] = SLIDE_DEC[0x80] = 0)."""
        for slide_mode in (0x80, 0xC0):
            self.assertEqual(predicted_slide_delta(slide_mode), 0)
            cols = {
                "WGh": 0x11,
                "TR": 0x80 | 60,
                "AD": 0x09,
                "SR": 0xA0,
                "AF": slide_mode,
            }
            song = _build_minimal_song(0, [(cols, 0x80)], _gate_on_pattern(60, 1))
            freqs = _render_voice_freq(song, 0, n_frames=6)
            # Same as no-slide: freq stays at NOTE_PITCH[60].
            for k, (fl, fh) in enumerate(freqs[1:], start=1):
                self.assertEqual(
                    (fl, fh),
                    (NOTE_PITCH_LO[60], NOTE_PITCH_HI[60]),
                    f"S={slide_mode:#x} frame {k}: boundary mode "
                    f"should produce no drift",
                )


class TestMultiRowSlideEngagement(unittest.TestCase):
    """Multi-row snippet with DL holds delay the slide. Row 0 holds
    without AF; row 1 sets AF. The slide engages at row 2's fetch.

    Empirically (cascade trace at DL_row1 = N):
        observed[0] = init (zeros)
        observed[1] = NOTE_PITCH (no slide yet)
        observed[2+] = NOTE_PITCH + (k - 1) * delta

    So observed[k] for k >= 2 shows the slide regardless of DL.
    This is because the cascade fetches row 1 at frame 0, then
    fetches row 2 at frame 1 (DL=0 means "fetch next frame"); larger
    DLs DO add hold frames, captured below for DL in {0, 1, 2}.
    """

    def test_dl_hold_delays_slide_engagement(self):
        note = 60
        slide_mode = 0xE0  # delta = -351, glow_worm's most common rate
        delta = predicted_slide_delta(slide_mode)
        for dl_row1 in (0, 1, 2, 3):
            with self.subTest(dl_row1=dl_row1):
                rows = [
                    (
                        {
                            "WGh": 0x11,
                            "TR": 0x80 | note,
                            "AD": 0x09,
                            "SR": 0xA0,
                        },
                        dl_row1,
                    ),
                    ({"AF": slide_mode}, 0x80),
                ]
                song = _build_minimal_song(0, rows, _gate_on_pattern(note, 1))
                freqs = _render_voice_freq(song, 0, n_frames=dl_row1 + 8)
                base = _base_freq(note)
                # observed[0] = init (zeros)
                self.assertEqual(freqs[0], (0, 0))
                # observed[1..1+dl_row1] = NOTE_PITCH (row 1 applied,
                # no AF yet; subsequent frames hold via DL).
                for k in range(1, 1 + dl_row1 + 1):
                    self.assertEqual(
                        freqs[k],
                        (NOTE_PITCH_LO[note], NOTE_PITCH_HI[note]),
                        f"DL={dl_row1} frame {k}: slide engaged early",
                    )
                # observed[2 + dl_row1] = first slide frame (= delta).
                # observed[2 + dl_row1 + j] = (j + 1) * delta.
                for k in range(2 + dl_row1, len(freqs)):
                    fl, fh = freqs[k]
                    obs = (fh << 8) | fl
                    drift = _s16(obs - base)
                    steps = k - (1 + dl_row1)
                    self.assertEqual(
                        drift,
                        steps * delta,
                        f"DL={dl_row1} frame {k}: drift {drift} != "
                        f"{steps}*{delta} = {steps * delta}",
                    )


class TestGateNResetsSlideState(unittest.TestCase):
    """A second GATE_N (re-trigger) zeros slide_mode + slide_accum
    (player line 990-995). After re-trigger to a no-AF snippet, the
    voice immediately returns to NOTE_PITCH[new_note]."""

    def test_retrigger_clears_slide(self):
        note1 = 60
        note2 = 72
        slide_mode = 0xE0
        # Row 1: slide-on snippet for note1.
        # Row 2: no-slide snippet for note2.
        rows = [
            (
                {
                    "WGh": 0x11,
                    "TR": 0x80 | note1,
                    "AD": 0x09,
                    "SR": 0xA0,
                    "AF": slide_mode,
                },
                0x80,
            ),
            (
                {"WGh": 0x11, "TR": 0x80 | note2, "AD": 0x09, "SR": 0xA0},
                0x80,
            ),
        ]
        # Pattern: GATE_N note1 with dur=5 (= 6-frame hold), then
        # GATE_N note2.
        pat = [
            PatternEvent(
                flag=PatternEvent.FLAG_GATE_N | PatternEvent.FLAG_GATE_A | 5,
                slot_a=1,
                slot_b=0,
                note=note1,
            ),
            PatternEvent(
                flag=PatternEvent.FLAG_GATE_N | PatternEvent.FLAG_GATE_A,
                slot_a=2,
                slot_b=0,
                note=note2,
            ),
        ]
        for _ in range(29):
            pat.append(PatternEvent.delay(15))
        pat.append(PatternEvent.alt_end(15))

        song = _build_minimal_song(0, rows, pat)
        freqs = _render_voice_freq(song, 0, n_frames=14)

        # Event 0 fires at frame 0 with dur=5 → row_timer=6. Pattern
        # advance decrements row_timer each frame, fetching event 1
        # when timer reaches 0. Trace: frames 1..6 decrement timer
        # 5→4→3→2→1→0, no fetch. Frame 7's pattern advance fetches
        # event 1 (the second GATE_N).
        #
        # observed[k] for k=1..7 shows slide accumulator at k * delta
        # (the slide integrates frames 0..6 while waiting for event 1).
        # Frame 7's GATE_N + row-2 apply means end-of-frame-7 freq =
        # NOTE_PITCH[note2]. observed[8] is the first post-retrigger
        # emit.
        base1 = _base_freq(note1)
        delta = predicted_slide_delta(slide_mode)
        for k in range(1, 8):
            fl, fh = freqs[k]
            obs = (fh << 8) | fl
            drift = _s16(obs - base1)
            self.assertEqual(
                drift,
                k * delta,
                f"pre-retrigger frame {k}: drift {drift} != {k * delta}",
            )

        # observed[8..] = NOTE_PITCH[note2] (slide cleared by GATE_N).
        for k in range(8, 14):
            fl, fh = freqs[k]
            self.assertEqual(
                (fl, fh),
                (NOTE_PITCH_LO[note2], NOTE_PITCH_HI[note2]),
                f"post-retrigger frame {k}: ({fl:#x},{fh:#x}) != "
                f"NOTE_PITCH[{note2}]; GATE_N didn't reset slide?",
            )


class TestGateOffPreservesSlideMode(unittest.TestCase):
    """Per the design: the slide accumulator integrates regardless of
    gate state. Only GATE_N resets slide_mode + accumulator. A WGh
    column flip that clears the gate bit (without firing GATE_N)
    must NOT disturb the slide.

    This is the property dump_to_defmon's slides path will rely on:
    the slide-active phrase may span the gate-off region between two
    GATE_N triggers.
    """

    def test_wgh_gate_off_does_not_reset_slide(self):
        note = 60
        slide_mode = 0xE0
        # Three-row snippet:
        #   row 1: gate-on + AF (slide engages)
        #   row 2: gate-off WGh (no AF, no TR)
        #   row 3: STop
        # No second GATE_N pattern event.
        rows = [
            (
                {
                    "WGh": 0x11,
                    "TR": 0x80 | note,
                    "AD": 0x09,
                    "SR": 0xA0,
                    "AF": slide_mode,
                },
                2,  # hold 3 frames then advance to row 2
            ),
            (
                {"WGh": 0x10},  # gate-bit cleared, sawtooth still on
                0x80,
            ),
        ]
        song = _build_minimal_song(0, rows, _gate_on_pattern(note, 1))
        freqs = _render_voice_freq(song, 0, n_frames=12)
        base = _base_freq(note)
        delta = predicted_slide_delta(slide_mode)

        # Slide engages at frame 1 (single-row AF in row 1) — the
        # emit-shift means frame 1's emit captures the post-row-1
        # pitch slide. Then DL=2 holds row 1 for 3 frames before
        # advancing to row 2. Through all of this the slide
        # integrates linearly with no reset.
        for k in range(1, len(freqs)):
            fl, fh = freqs[k]
            obs = (fh << 8) | fl
            drift = _s16(obs - base)
            self.assertEqual(
                drift,
                k * delta,
                f"frame {k}: drift {drift} != {k * delta}; "
                f"WGh gate-off should not disturb slide",
            )


class TestSlideDeltaInvertible(unittest.TestCase):
    """SLIDE_DELTAS is the lookup that reverse-mapping uses. Verify
    it's deterministic and contains the expected boundary cases."""

    def test_slide_delta_unique_count(self):
        """Across all 128 active modes ($80..$FF), how many distinct
        delta values are there? Audit data point for the design doc."""
        deltas = {predicted_slide_delta(s) for s in range(0x80, 0x100)}
        # Documenting the population at landing time:
        # On the embedded LUT this is in the low tens, not 128 (many
        # adjacent modes share the same delta byte).
        self.assertGreater(len(deltas), 1, "all active modes shouldn't have delta = 0")
        self.assertLessEqual(
            len(deltas), 128, "more distinct deltas than possible modes"
        )

    def test_slide_delta_symmetry(self):
        """slide_mode S and S + 0x40 produce opposite-sign deltas
        with equal magnitude (positive vs negative slide on the same
        LUT byte pair)."""
        for s_pos in range(0x80, 0xC0):
            s_neg = s_pos + 0x40
            self.assertEqual(
                predicted_slide_delta(s_pos),
                -predicted_slide_delta(s_neg),
                f"asymmetry at S={s_pos:#x} vs {s_neg:#x}",
            )


class TestPSOscillator(unittest.TestCase):
    """Foundational tests for the PS (pulse-width sweep) oscillator.

    The PS column in a sidTAB row sets ``voice.ps_depth``. The PS
    oscillator (player line 803) runs after the pitch slide each
    frame and integrates pulse_lo/pulse_hi by:

      * depth $00, $80: no-op (delta = 0).
      * depth $81..$FF: ADD path. delta = depth & $7F per frame.
        pulse_lo += magnitude with carry into pulse_hi.
      * depth $01..$7F: SUB path. delta = -(depth + 1) per frame
        (C-in=0 forces SBC to decrement by depth+1, not depth).

    Unlike slide_mode, PS depth is NOT reset by GATE_N. It persists
    until overwritten by another PS column or until the boundary
    clamp at $14B4/$14C2 flips bit 7."""

    ADD_MODES = (0x81, 0x88, 0x90, 0xA0, 0xB0, 0xC0, 0xD0, 0xE0, 0xF0, 0xFE, 0xFF)
    SUB_MODES = (0x01, 0x08, 0x10, 0x20, 0x40, 0x60, 0x7E, 0x7F)

    def _build(self, ps_depth: int, pw_byte: int = 0x10):
        cols = {
            "WGh": 0x41,
            "TR": 0x80 | 60,
            "AD": 0x09,
            "SR": 0xA0,
            "PW": pw_byte,
            "PS": ps_depth,
        }
        return _build_minimal_song(0, [(cols, 0x80)], _gate_on_pattern(60, 1))

    def test_ps_zero_no_sweep(self):
        for d in (0x00, 0x80):
            with self.subTest(depth=d):
                song = self._build(d)
                pws = _render_voice_pw(song, 0, n_frames=6)
                # frame 0 = init; frames 1+ = constant PW.
                first = pws[1]
                for k in range(2, 6):
                    self.assertEqual(
                        pws[k],
                        first,
                        f"PS=$00 frame {k}: PW changed ({pws[k]}) vs first ({first})",
                    )

    def test_ps_add_linear_increment(self):
        for d in self.ADD_MODES:
            with self.subTest(depth=d):
                song = self._build(d)
                pws = _render_voice_pw(song, 0, n_frames=6)
                expected = predicted_ps_delta(d)
                self.assertEqual(expected, d & 0x7F)
                for k in range(2, 6):
                    prev = (pws[k - 1][1] << 8) | pws[k - 1][0]
                    cur = (pws[k][1] << 8) | pws[k][0]
                    diff = (cur - prev) & 0xFFFF
                    if diff >= 0x8000:
                        diff -= 0x10000
                    self.assertEqual(
                        diff,
                        expected,
                        f"PS=${d:02X} frame {k}: diff {diff} != {expected}",
                    )

    def test_ps_sub_linear_decrement(self):
        for d in self.SUB_MODES:
            with self.subTest(depth=d):
                song = self._build(d)
                pws = _render_voice_pw(song, 0, n_frames=6)
                expected = predicted_ps_delta(d)
                self.assertEqual(expected, -(d + 1))
                for k in range(2, 6):
                    prev = (pws[k - 1][1] << 8) | pws[k - 1][0]
                    cur = (pws[k][1] << 8) | pws[k][0]
                    diff = (cur - prev) & 0xFFFF
                    if diff >= 0x8000:
                        diff -= 0x10000
                    self.assertEqual(
                        diff,
                        expected,
                        f"PS=${d:02X} frame {k}: diff {diff} != {expected}",
                    )

    def test_predicted_ps_delta_round_trip(self):
        """``ps_depth_from_delta(predicted_ps_delta(d))`` == d for all
        non-zero deltas; None for deltas of 0 (== no PS)."""
        for d in range(0x100):
            delta = predicted_ps_delta(d)
            if delta == 0:
                self.assertIsNone(ps_depth_from_delta(delta))
            else:
                self.assertEqual(
                    ps_depth_from_delta(delta),
                    d,
                    f"round-trip fail at depth=${d:02X} delta={delta}",
                )


if __name__ == "__main__":
    unittest.main()
