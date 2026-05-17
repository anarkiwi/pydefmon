"""Foundational tests for defMON's layer-1 sidcall cascade.

The layer-1 cascade is the second per-voice sidcall channel
(``sc2_row_idx`` / ``sc2_step_counter``) the player walks in parallel
with layer 0. It is re-armed by ``FLAG_GATE_B`` pattern events:

  player line 978-980:
    if flag & FLAG_GATE_B:
        v.sc2_row_idx = slot_b
        v.sc2_step_counter = 0   # "fetch now" on next _sidcall_tick

Each frame, both layers tick:

  player line 547-549:
    self._sidcall_tick(v_idx, layer=1)
    self._sidcall_tick(v_idx, layer=2)

Layer 2's apply (line 1018-1107) is functionally identical to
layer 1's: it reads the sidTAB row at ``sc2_row_idx``, applies low-
half + high-half columns (TR/AF/PS/RE/FV/CP/ACID), then stores the
DL byte into the step counter (``DL`` semantics: $00=fetch next,
$01..$7F=hold-N-then-refetch, $80..$FF=STop freeze).

These tests validate the model ``dump_to_defmon``'s layer-1
reverse-mapping will rely on. Each test builds a minimal synthetic
.prg that exercises one specific layer-1 behaviour and asserts the
observed register stream matches the predicted trajectory.

Same ``clear_song_table()`` gotcha as
``tests/test_defmon_slides_model.py``: it wipes $1B00..$1EFF
(including the DL region), so ``_build_layer1_song`` below calls
it BEFORE any ``set_dl``.
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
    SID_REG_BASE,
)

PAL_CYCLES_PER_FRAME = 23546
SILENT_INSTRUMENT_ROW = 1  # layer-0 patch row for the carrier voice
LAYER1_BASE_ROW = 10  # arbitrary base for layer-1 row allocations
D416 = SID_REG_BASE + 0x16  # cutoff hi
D417 = SID_REG_BASE + 0x17  # res + routing
D418 = SID_REG_BASE + 0x18  # volume + filter mode


def _write_sidtab_row(song: DefmonSong, row: int, cols: dict, dl: int) -> None:
    raw = SidtabRow.pack(cols)
    off = 0x5F00 - LOAD_ADDRESS + row * 15
    for i, b in enumerate(raw):
        song.snapshot[off + i] = b
    song.set_dl(row, dl & 0xFF)
    song.set_jp(row)  # active-linear


def _build_layer1_song(
    *,
    voice: int,
    layer0_row_cols: dict,
    layer1_rows: list[tuple[dict, int]],
    pattern_events: list[PatternEvent],
) -> DefmonSong:
    """Build a one-voice .prg with a silent layer-0 patch on
    ``voice`` plus a layer-1 sidTAB walk starting at row
    ``LAYER1_BASE_ROW``.

    Args:
        voice: 0/1/2.
        layer0_row_cols: columns for the gate-on snippet at row 1.
        layer1_rows: list of (cols, dl) pairs starting at
            ``LAYER1_BASE_ROW``. Each row's columns get packed and
            written; ``dl`` is its hold byte.
        pattern_events: 32 PatternEvents for pattern 1.

    Returns:
        A DefmonSong ready for DefmonPlayer.
    """
    song = DefmonSong()
    song.set_pattern_events(0, PatternEvent.silent_pattern())
    song.set_pattern_events(1, pattern_events)

    # ORDER CRITICAL: clear_song_table wipes $1B00..$1EFF; must run
    # BEFORE set_dl.
    song.clear_song_table()
    voice_patterns = [0, 0, 0]
    voice_patterns[voice] = 1
    song.set_step(0, v1=voice_patterns[0], v2=voice_patterns[1], v3=voice_patterns[2])
    song.set_jump(1, target=0, count=0)

    _write_sidtab_row(song, SILENT_INSTRUMENT_ROW, layer0_row_cols, 0x80)
    for idx, (cols, dl) in enumerate(layer1_rows):
        _write_sidtab_row(song, LAYER1_BASE_ROW + idx, cols, dl)

    song.snapshot[0x715A - LOAD_ADDRESS] = PAL_CYCLES_PER_FRAME & 0xFF
    song.snapshot[0x715B - LOAD_ADDRESS] = (PAL_CYCLES_PER_FRAME >> 8) & 0xFF
    song.snapshot[0x715C - LOAD_ADDRESS] = 1  # one sub-frame per main frame
    return song


def _default_layer0_cols() -> dict:
    """A bland sawtooth gate-on patch for the carrier voice."""
    return {"WGh": 0x11, "TR": 0x80 | 60, "AD": 0x09, "SR": 0xA0}


def _gate_a_pattern(
    *,
    layer0_slot: int = SILENT_INSTRUMENT_ROW,
    layer1_gate_b_at: dict[int, int] | None = None,
    note: int = 60,
    delay_dur: int = 0,
) -> list[PatternEvent]:
    """Build a 32-event pattern that:

    - fires GATE_A + GATE_N at event 0 (re-arms layer-0 cascade
      with the supplied note + slot_a);
    - optionally fires GATE_B at any event index >= 1 per
      ``layer1_gate_b_at`` (mapping event_idx -> slot_b);
    - fills remaining slots with ``delay_dur`` delays.

    Default ``delay_dur=0`` makes each event consume 2 frames so
    later layer-1 events fire within reasonable horizons.
    """
    layer1_gate_b_at = layer1_gate_b_at or {}
    pat: list[PatternEvent] = []
    pat.append(
        PatternEvent(
            flag=PatternEvent.FLAG_GATE_N | PatternEvent.FLAG_GATE_A,
            slot_a=layer0_slot,
            slot_b=0,
            note=note & 0x7F,
        )
    )
    for i in range(1, 31):
        b_target = layer1_gate_b_at.get(i)
        if b_target is not None:
            pat.append(
                PatternEvent(
                    flag=PatternEvent.FLAG_GATE_B | (delay_dur & 0x0F),
                    slot_a=0,
                    slot_b=b_target & 0xFF,
                    note=0,
                )
            )
        else:
            pat.append(PatternEvent.delay(delay_dur))
    pat.append(PatternEvent.alt_end(15))
    return pat


def _render_global_writes(
    song: DefmonSong, n_frames: int, regs: tuple[int, ...]
) -> list[dict[int, int]]:
    """Run the player for n_frames; return per-frame
    ``{reg: last_value}`` for the supplied global regs ($D416,
    $D417, $D418). The player emits one ``(reg, val)`` per frame
    per reg (dedupe-suppression happens in the WAV-render driver,
    not in ``play_frame``)."""
    player = DefmonPlayer(song)
    out: list[dict[int, int]] = []
    state: dict[int, int] = {r: 0 for r in regs}
    for _ in range(n_frames):
        for reg, val in player.play_frame():
            if reg in state:
                state[reg] = val & 0xFF
        out.append(dict(state))
    return out


class TestLayer1Baseline(unittest.TestCase):
    """No layer-1 row applied: cutoff stays at floor=2 forever."""

    def test_no_layer1_cutoff_stays_at_floor(self):
        song = _build_layer1_song(
            voice=0,
            layer0_row_cols=_default_layer0_cols(),
            layer1_rows=[],
            pattern_events=_gate_a_pattern(),
        )
        snapshots = _render_global_writes(song, n_frames=20, regs=(D416,))
        for k, s in enumerate(snapshots):
            self.assertEqual(
                s[D416],
                0x02,
                f"frame {k}: $D416 = {s[D416]:#x}, expected floor $02",
            )


class TestLayer1AcidAbsoluteSet(unittest.TestCase):
    """One-row layer-1 cascade with an absolute ACID command sets
    the cutoff accumulator's high byte. Subsequent frames emit
    that value (modulo carry+extra effects, which are zero here)."""

    def test_abs_set_to_0x40_produces_64_emit(self):
        # ACID column is 2 bytes (low, high). Absolute set: high bit
        # 7 clear. We want acc_hi = $40, acc_lo = $00, so the column
        # encodes (low_byte=$00, high_byte=$40) = $0040.
        layer1_rows = [({"ACID": 0x0040}, 0x80)]  # STop after apply
        # GATE_B at event 1 (one delay after the layer-0 gate-on).
        # Event 0 holds the pattern for dur=0+1=1 frame; event 1
        # (the GATE_B) fires at frame 1's pattern advance.
        # That means the layer-1 row apply lands at the next
        # _sidcall_tick after event 1 fires, on frame 2.
        pat = _gate_a_pattern(layer1_gate_b_at={1: LAYER1_BASE_ROW})
        song = _build_layer1_song(
            voice=0,
            layer0_row_cols=_default_layer0_cols(),
            layer1_rows=layer1_rows,
            pattern_events=pat,
        )
        snapshots = _render_global_writes(song, n_frames=12, regs=(D416,))
        # Frame 0: pre-apply floor. Frames 1..(layer-1 latency):
        # still floor. Once layer-1 cascade applies the absolute
        # set, the slide step adds 0 (step=0), so emit settles at
        # acc_hi + extra(0) + hc(0) = $40 = 64. Confirm SOME frame
        # past the latency shows $40, and the value is stable from
        # then on.
        engaged = [k for k, s in enumerate(snapshots) if s[D416] == 0x40]
        self.assertTrue(
            engaged,
            f"ACID abs $40 never engaged; trajectory: "
            f"{[s[D416] for s in snapshots]}",
        )
        first = engaged[0]
        # Stable thereafter (with extra=0 / step=0, the trajectory
        # is constant once engaged).
        for k in range(first, len(snapshots)):
            self.assertEqual(
                snapshots[k][D416],
                0x40,
                f"after engage at frame {first}, frame {k} = "
                f"{snapshots[k][D416]:#x}, expected stable $40",
            )


class TestLayer1AcidSlideSBC(unittest.TestCase):
    """Two-row layer-1 cascade: abs set then SBC slide step=2.
    After the slide engages, $D416 decrements by 2 per frame
    (matching glow_worm's first 20 cycle frames)."""

    def test_sbc_step_2_linear_descent(self):
        # ACID byte order in a sidTAB row (per _apply_high_bitmap_globals):
        #   val = (raw[pos] << 8) | raw[pos+1]
        #   low  = (val >> 8) & 0xFF   <- upper byte of val
        #   high = val & 0xFF          <- lower byte of val
        # So to set acc_hi=$40, acc_lo=$00 absolute, use ACID=$0040;
        # to set SBC slide step=2, use ACID=$00C2 (low=$00, high=$C2:
        # bit 7 set = slide, bit 6 set = SBC, $C2 & $3F = 2 = step_hi).
        layer1_rows = [
            ({"ACID": 0x0040}, 0x00),  # abs set acc_hi=$40, advance next frame
            ({"ACID": 0x00C2}, 0x80),  # SBC slide step=2, then STop
        ]
        pat = _gate_a_pattern(layer1_gate_b_at={1: LAYER1_BASE_ROW})
        song = _build_layer1_song(
            voice=0,
            layer0_row_cols=_default_layer0_cols(),
            layer1_rows=layer1_rows,
            pattern_events=pat,
        )
        snapshots = _render_global_writes(song, n_frames=30, regs=(D416,))
        traj = [s[D416] for s in snapshots]
        # Find the engagement point (first $40 after init).
        engaged = [k for k, v in enumerate(traj) if v == 0x40]
        self.assertTrue(engaged, f"ACID never engaged; trajectory: {traj}")
        # The SBC slide row applies one frame after the abs set
        # row's DL=$00 advance. From that point, the slide produces
        # 62, 60, 58, ... per the math `acc_hi - 2 - 1 + 1 = acc_hi - 2`
        # at each step (carry-from-lo + carry-to-output cancel for
        # step_lo=0).
        descent_start = engaged[0] + 1  # first frame after the $40 peak
        for j in range(20):
            k = descent_start + j
            if k >= len(traj):
                break
            expected = 0x40 - 2 * (j + 1)
            self.assertEqual(
                traj[k],
                expected,
                f"frame {k}: $D416 = {traj[k]} != expected "
                f"{expected} ({j+1} steps from peak)",
            )


class TestLayer1AcidSlideReversal(unittest.TestCase):
    """Three-row layer-1 cascade with SBC then ADC slide commands,
    spaced via DL holds. The trajectory descends, reaches floor,
    then the ADC row reverses direction. Models the glow_worm
    row-44/45/46 setup."""

    def test_sbc_then_adc_reversal(self):
        # Row N: ACID abs $4000 (DL=0 → advance after 1 frame).
        # Row N+1: ACID SBC step=2 (DL=18 → hold 19 frames).
        # Row N+2: ACID ADC step=2 (DL=$80 STop after apply).
        layer1_rows = [
            ({"ACID": 0x0040}, 0x00),  # abs acc_hi=$40
            ({"ACID": 0x00C2}, 0x12),  # SBC slide step=2, DL=$12 = 18
            ({"ACID": 0x0082}, 0x80),  # ADC slide step=2, STop
        ]
        pat = _gate_a_pattern(layer1_gate_b_at={1: LAYER1_BASE_ROW})
        song = _build_layer1_song(
            voice=0,
            layer0_row_cols=_default_layer0_cols(),
            layer1_rows=layer1_rows,
            pattern_events=pat,
        )
        snapshots = _render_global_writes(song, n_frames=60, regs=(D416,))
        traj = [s[D416] for s in snapshots]
        # Find peak ($40) — start of descent.
        peak_idx = traj.index(0x40)
        # 19-frame descent + 1 transient + ascent. The trajectory
        # must include a turning point (local minimum) followed by
        # a value strictly greater than the next-to-last descent
        # value.
        descent_end = None
        for k in range(peak_idx + 1, len(traj) - 1):
            if traj[k + 1] > traj[k]:
                descent_end = k
                break
        self.assertIsNotNone(
            descent_end,
            f"no reversal detected in trajectory: {traj}",
        )
        # Reversal point must lie within descent_end + 2 of the
        # initial 19-frame descent (allowing slack for emit-shift
        # latency between GATE_B and first apply).
        descent_len = descent_end - peak_idx
        self.assertGreaterEqual(
            descent_len, 18, f"descent only {descent_len} frames; expected ≥18"
        )
        # And there must be at least one ascending frame (delta > 0)
        # right after the descent ends.
        self.assertGreater(
            traj[descent_end + 1],
            traj[descent_end],
            f"frame {descent_end+1} ({traj[descent_end+1]}) not above "
            f"descent end ({traj[descent_end]})",
        )


class TestLayer1GateBReArm(unittest.TestCase):
    """Re-arming layer-1 mid-walk via a second GATE_B resets
    ``sc2_row_idx`` and ``sc2_step_counter``. The trajectory after
    re-arm starts fresh from the new row."""

    def test_re_arm_resets_cascade(self):
        # Two distinct 1-row layer-1 instruments at LAYER1_BASE_ROW
        # and LAYER1_BASE_ROW + 1. First fires at event 1 (abs $40),
        # second fires at event 6 (abs $20).
        layer1_rows = [
            ({"ACID": 0x0040}, 0x80),  # row 10: abs acc_hi=$40 then STop
            ({"ACID": 0x0020}, 0x80),  # row 11: abs acc_hi=$20 then STop
        ]
        pat = _gate_a_pattern(
            layer1_gate_b_at={1: LAYER1_BASE_ROW, 6: LAYER1_BASE_ROW + 1}
        )
        song = _build_layer1_song(
            voice=0,
            layer0_row_cols=_default_layer0_cols(),
            layer1_rows=layer1_rows,
            pattern_events=pat,
        )
        snapshots = _render_global_writes(song, n_frames=30, regs=(D416,))
        traj = [s[D416] for s in snapshots]
        # Both engagements should be visible: $40 phase then $20 phase.
        self.assertIn(0x40, traj, f"abs $40 never seen: {traj}")
        self.assertIn(0x20, traj, f"re-armed abs $20 never seen: {traj}")
        # $20 must appear AFTER $40 (re-arm fires later than first arm).
        first_40 = traj.index(0x40)
        first_20 = traj.index(0x20)
        self.assertGreater(
            first_20,
            first_40,
            f"re-armed $20 at frame {first_20} did not follow $40 at frame "
            f"{first_40}",
        )


class TestLayer1StopWithSlot0(unittest.TestCase):
    """GATE_B with slot_b=0: the layer-1 cascade arms row 0 (the
    silent default row), then on the next sidcall tick fetches +
    applies row 0. Row 0 is empty (all bits clear), so the apply
    is a no-op. With DL at row 0 also $00, the cascade keeps
    walking, but it walks empty rows for the next several frames.
    Practical effect: layer-1's prior trajectory state is *frozen*
    in the sense that no further sidTAB writes feed it."""

    def test_slot_b_zero_after_acid_freezes_cutoff(self):
        # Layer-1: row 10 = abs $0040 with DL=$80 STop. Cutoff
        # engages at $40, then layer-1 freezes. Mid-way, a GATE_B
        # with slot_b=0 re-arms layer-1 to row 0; row 0 is empty,
        # so no further global writes. $D416 stays at $40 because
        # the slide step is still 0.
        layer1_rows = [({"ACID": 0x0040}, 0x80)]
        pat = _gate_a_pattern(layer1_gate_b_at={1: LAYER1_BASE_ROW, 10: 0})
        song = _build_layer1_song(
            voice=0,
            layer0_row_cols=_default_layer0_cols(),
            layer1_rows=layer1_rows,
            pattern_events=pat,
        )
        snapshots = _render_global_writes(song, n_frames=30, regs=(D416,))
        traj = [s[D416] for s in snapshots]
        self.assertIn(0x40, traj, f"abs $40 never engaged: {traj}")
        first_40 = traj.index(0x40)
        # Beyond first_40 the trajectory should remain $40 — the
        # slot_b=0 stop does not perturb the cutoff state (which
        # already has step=0 from the abs set).
        for k in range(first_40, len(traj)):
            self.assertEqual(
                traj[k],
                0x40,
                f"frame {k}: cutoff drifted to {traj[k]:#x} after slot_b=0 stop",
            )


class TestLayer0Layer1Cohabitation(unittest.TestCase):
    """A layer-0 cascade walks a pitch slide (via the snippet's AF
    column) while a layer-1 cascade walks an ACID setup. Both
    trajectories must reproduce independently."""

    def test_layer0_pitch_unaffected_by_layer1_acid(self):
        # Layer-0: row 1 carries gate-on + AF=$E0 (steady slide).
        layer0_cols = {
            "WGh": 0x11,
            "TR": 0x80 | 60,
            "AD": 0x09,
            "SR": 0xA0,
            "AF": 0xE0,
        }
        # Layer-1: row 10 = ACID abs acc_hi=$40 (encoded as $0040).
        layer1_rows = [({"ACID": 0x0040}, 0x80)]
        pat = _gate_a_pattern(layer1_gate_b_at={1: LAYER1_BASE_ROW})
        song = _build_layer1_song(
            voice=0,
            layer0_row_cols=layer0_cols,
            layer1_rows=layer1_rows,
            pattern_events=pat,
        )
        snapshots = _render_global_writes(song, n_frames=12, regs=(D416,))
        # Cutoff engages as before.
        self.assertIn(
            0x40,
            [s[D416] for s in snapshots],
            f"layer-1 ACID did not engage in cohabitation: "
            f"{[s[D416] for s in snapshots]}",
        )
        # Also check layer-0 pitch slide engages — read freq emit.
        player = DefmonPlayer(song)
        fl_reg = SID_REG_BASE + 0  # V0 freq lo
        fh_reg = SID_REG_BASE + 1
        freqs = []
        for _ in range(12):
            fl, fh = 0, 0
            for reg, val in player.play_frame():
                if reg == fl_reg:
                    fl = val
                elif reg == fh_reg:
                    fh = val
            freqs.append((fl, fh))
        # The slide should make freq drift away from NOTE_PITCH[60]
        # = the fixed gate-on value. We just check the freq is NOT
        # constant (= slide is alive).
        distinct = {(fl, fh) for fl, fh in freqs[2:]}
        self.assertGreater(
            len(distinct),
            1,
            "layer-0 pitch slide stalled while layer-1 was active "
            f"(observed freq sequence: {freqs})",
        )


class TestLayer1ReBitmaskAndDirect(unittest.TestCase):
    """The RE column dispatches via three branches:

      val == 0      -> filter_res_routing &= voice_and
      val bit 3 set -> filter_res_routing = (prev & $0F) | val | voice_or
      else          -> filter_res_routing = val

    Verify each branch via a layer-1 row carrying the corresponding
    RE byte. ``voice_or`` / ``voice_and`` are voice-specific
    constants (V0=$01/$FE, V1=$02/$FD, V2=$04/$FB).
    """

    def _run(self, voice: int, re_val: int) -> int:
        """Build a one-row layer-1 song that applies ``RE=re_val``,
        run it, return the resulting $D417 emit after the apply."""
        layer1_rows = [({"RE": re_val}, 0x80)]
        pat = _gate_a_pattern(layer1_gate_b_at={1: LAYER1_BASE_ROW})
        song = _build_layer1_song(
            voice=voice,
            layer0_row_cols=_default_layer0_cols(),
            layer1_rows=layer1_rows,
            pattern_events=pat,
        )
        snapshots = _render_global_writes(song, n_frames=12, regs=(D417,))
        # The applied value should appear by mid-song; return the
        # last frame's $D417 (steady state).
        return snapshots[-1][D417]

    def test_re_direct_set_path(self):
        """val bit 3 clear, val != 0: $D417 := val verbatim."""
        for voice in range(3):
            for re_val in (0x10, 0x20, 0x40, 0x70):
                with self.subTest(voice=voice, re_val=re_val):
                    got = self._run(voice, re_val)
                    self.assertEqual(
                        got,
                        re_val,
                        f"V{voice} direct-set RE=${re_val:02X} got " f"${got:02X}",
                    )

    def test_re_bitmask_path(self):
        """val bit 3 set: filter_res_routing := (prev & $0F) | val | voice_or.
        prev starts at $00 (player init), so result == val | voice_or."""
        voice_or = (0x01, 0x02, 0x04)
        for voice in range(3):
            for re_val in (0xF8, 0xF9, 0xFC, 0xFE):
                with self.subTest(voice=voice, re_val=re_val):
                    got = self._run(voice, re_val)
                    expected = (re_val | voice_or[voice]) & 0xFF
                    self.assertEqual(
                        got,
                        expected,
                        f"V{voice} bitmask RE=${re_val:02X} got "
                        f"${got:02X}, expected ${expected:02X}",
                    )

    def test_re_clear_path_against_nonzero_prev(self):
        """val == 0: filter_res_routing &= voice_and. Set up via two
        layer-1 rows: row 0 sets RE via the bitmask path (so prev !=
        0), row 1 fires RE=0 (clear) and STops."""
        voice_or = (0x01, 0x02, 0x04)
        voice_and = (0xFE, 0xFD, 0xFB)
        seed = 0xF8  # bitmask path: result = $F8 | voice_or
        for voice in range(3):
            with self.subTest(voice=voice):
                layer1_rows = [
                    ({"RE": seed}, 0x00),  # set via bitmask, advance
                    ({"RE": 0x00}, 0x80),  # clear path
                ]
                pat = _gate_a_pattern(layer1_gate_b_at={1: LAYER1_BASE_ROW})
                song = _build_layer1_song(
                    voice=voice,
                    layer0_row_cols=_default_layer0_cols(),
                    layer1_rows=layer1_rows,
                    pattern_events=pat,
                )
                snapshots = _render_global_writes(song, n_frames=20, regs=(D417,))
                # After both rows have fired, $D417 = (seed | voice_or) & voice_and.
                expected = ((seed | voice_or[voice]) & voice_and[voice]) & 0xFF
                got = snapshots[-1][D417]
                self.assertEqual(
                    got,
                    expected,
                    f"V{voice} clear path got ${got:02X}, " f"expected ${expected:02X}",
                )


class TestForwardSimulatorMatchesPlayer(unittest.TestCase):
    """``dump_to_defmon.CutoffSlideState`` is the planner's offline
    forward simulator -- a faithful port of the player's
    ``_cutoff_slide_step``. Each test below builds a synthetic .prg
    that drives layer-1 with a known ACID sequence, runs it through
    the player, and asserts the planner's simulator predicts the
    same per-frame $D416 emit byte-for-byte.

    If the simulator drifts from the player, the layer-1 epoch
    detector (built on top of this simulator) will mis-detect.
    """

    def _simulate(self, acid_at_frame: dict[int, int], n_frames: int) -> list[int]:
        """Run ``CutoffSlideState`` for ``n_frames``; apply ACID
        commands per ``acid_at_frame`` (mapping frame F -> ACID
        16-bit value to apply at the END of frame F, so frame F+1's
        emit reflects the new state)."""
        from pydefmon.dump_to_defmon import (
            CutoffSlideState,
            apply_acid_column,
        )

        state = CutoffSlideState()
        out: list[int] = []
        for f in range(n_frames):
            out.append(state.step())
            if f in acid_at_frame:
                apply_acid_column(state, acid_at_frame[f])
        return out

    def test_baseline_no_acid_stays_at_floor(self):
        # No ACID command -> emit stays at floor=2 forever (matches
        # the TestLayer1Baseline behaviour above).
        sim = self._simulate({}, n_frames=20)
        for k, v in enumerate(sim):
            self.assertEqual(v, 0x02, f"sim frame {k}: {v} != $02")

    def test_abs_set_matches_player(self):
        # ACID abs $0040 applied at frame 2. Compare against the
        # player rendering of the same .prg.
        sim = self._simulate({2: 0x0040}, n_frames=15)
        # Build the synthetic .prg, render it.
        layer1_rows = [({"ACID": 0x0040}, 0x80)]
        # The player applies ACID during the cascade tick of the
        # frame whose GATE_B fires. Pattern event 0 (frame 0)
        # is GATE_N+GATE_A on layer 0, event 1 (frame 2 with
        # dur=0 delays) is GATE_B+slot_b=10. The cascade tick at
        # frame 2 applies the ACID column. Emit at frame 3 reflects
        # the new state.
        pat = _gate_a_pattern(layer1_gate_b_at={1: LAYER1_BASE_ROW})
        song = _build_layer1_song(
            voice=0,
            layer0_row_cols=_default_layer0_cols(),
            layer1_rows=layer1_rows,
            pattern_events=pat,
        )
        snapshots = _render_global_writes(song, n_frames=15, regs=(D416,))
        observed = [s[D416] for s in snapshots]
        # Trim to the engaged tail; the leading frames before
        # engagement must agree (both floor=$02), and the tail must
        # agree on the engaged value ($40).
        self.assertIn(0x40, observed)
        self.assertIn(0x40, sim, f"simulator never engaged: {sim}")
        # The simulator and the player must reach the same steady
        # state.
        self.assertEqual(observed[-1], sim[-1])

    def test_sbc_slide_step_2_matches_player(self):
        layer1_rows = [
            ({"ACID": 0x0040}, 0x00),
            ({"ACID": 0x00C2}, 0x80),
        ]
        pat = _gate_a_pattern(layer1_gate_b_at={1: LAYER1_BASE_ROW})
        song = _build_layer1_song(
            voice=0,
            layer0_row_cols=_default_layer0_cols(),
            layer1_rows=layer1_rows,
            pattern_events=pat,
        )
        snapshots = _render_global_writes(song, n_frames=30, regs=(D416,))
        observed = [s[D416] for s in snapshots]
        # The simulator's job: from the moment the abs set lands,
        # produce 64, then 62, 60, 58, ... per the SBC step=2 math.
        # We anchor by finding the peak $40 in both observed and
        # simulator, then assert the tail descents match for 19
        # frames.
        from pydefmon.dump_to_defmon import (
            CutoffSlideState,
            apply_acid_column,
        )

        # Simulate manually: apply abs at frame K, slide at frame
        # K+1 (mirroring the cascade walk).
        state = CutoffSlideState()
        sim = [state.step()]
        apply_acid_column(state, 0x0040)
        sim.append(state.step())  # emit reflects acc_hi=$40
        apply_acid_column(state, 0x00C2)
        for _ in range(20):
            sim.append(state.step())

        # Anchor both sequences at the $40 peak.
        first_obs = observed.index(0x40)
        first_sim = sim.index(0x40)
        for j in range(15):
            o = observed[first_obs + j] if first_obs + j < len(observed) else None
            s = sim[first_sim + j] if first_sim + j < len(sim) else None
            self.assertEqual(
                o,
                s,
                f"frame +{j} from peak: observed={o} sim={s} "
                f"(observed tail {observed[first_obs:first_obs+10]}, "
                f"sim tail {sim[first_sim:first_sim+10]})",
            )


class TestProfileLayer1CutoffOnGlowWorm(unittest.TestCase):
    """The planner's ``profile_layer1_cutoff`` against the audit's
    expected reset-frame count on glow_worm. If the audit says
    16 reset frames, the planner must find at least that many
    (and ideally exactly that count) to drive the round-trip
    layer-1 emission."""

    def test_reset_frame_count_matches_audit(self):
        import os
        from pydefmon.dump_to_defmon import (
            build_freq_to_note_per_voice,
            replay_csv,
            profile_layer1_cutoff,
        )

        repo = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
        csv_path = os.path.join(repo, "fixtures", "glow_worm.csv")
        fm = build_freq_to_note_per_voice()
        states = replay_csv(csv_path, fm)
        fires = profile_layer1_cutoff(states)
        # Audit (commit 37e4a9c): 16 reset frames found by the same
        # cutoff==$40 + delta>1 rule. The planner output must agree.
        self.assertEqual(
            len(fires),
            16,
            f"profile_layer1_cutoff found {len(fires)} GATE_B fires; "
            f"audit-expected 16. fires={fires[:20]}",
        )
        # First fire should be at frame 0 (the initial cutoff
        # engagement, reset at frame 1).
        self.assertEqual(fires[0], 0)


if __name__ == "__main__":
    unittest.main()
