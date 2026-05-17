# dump_to_defmon slides — design note

## What

Lift CSV → .prg reconstruction recall by reverse-mapping observed
off-LUT freq trajectories to defMON's `AF` (slide_mode) column,
emitting them as multi-row sidTAB snippets so the slide engages at
the same frame the source authored it.

## Why

Per-register recall on glow_worm (baseline 832/14236 = 5.84%):

| class | matched | source | recall |
|---|---|---|---|
| V0.freq | 97 | 2239 | 4.33% |
| V1.freq | 119 | 909 | 13.09% |
| V2.freq | 242 | 1451 | 16.68% |
| **freq total** | **458** | **4599** | **~10%** |

Freq misses account for ~4141 of the 13404 total misses (~31%) and
are the largest single recall lever. The current `snap_note` snaps
off-LUT freqs to `prev_note` and emits no slide — those frames'
freq writes are systematically missed.

## Empirical refutation of "AF column at gate-on"

The AGENTS.md backlog item proposed setting `AF` on the gate-on
snippet. Inspecting glow_worm V0 phrase at frame 384 (note 60,
`NOTE_PITCH[60] = 2228`):

```
+0  f=384: 2228 [LUT]  g=1   <- gate-on, NOTE_PITCH[60]
+1  f=385: 2228 [LUT]  g=1   <- still NOTE_PITCH[60] (no slide yet)
+2  f=386: 1877 [off]  g=0   <- gate-off; slide engages, -351/frame
+3  f=387: 1526 [off]  g=0
+4  f=388: 1175 [off]  g=0
...
+11 f=395: 64254 [off] g=0
+12 f=396: 35641 [LUT] g=1   <- next gate-on, new note
```

The slide is a perfectly linear -351 freq/frame active slide. But:

1. **It engages two frames after gate-on**, not at gate-on. Setting
   `AF` on the gate-on row would drift starting frame 384, missing
   the source's 2228 anchors at frames 384-385.
2. **It spans the gate-off region** (386-395). The slide accumulator
   integrates regardless of gate; only `GATE_N` resets it.

These two properties mean the slide must be encoded as a delayed
column flip in a multi-row snippet, AND the gate-off must NOT fire
`GATE_N` (which would zero the accumulator and break the slide).

Most glow_worm V0 slides appear to follow this "release slide"
pattern (-351 per frame typical, terminating at the next gate-on).
Single-row "`AF` at gate-on" gives near-zero recall lift.

## Proposed approach

Three coupled changes to `pydefmon/dump_to_defmon.py`:

### 1. Multi-row instrument snippets

`Instrument.deltas` is already `tuple[(rel, cols), ...]` and
`lay_snippets` already lays multi-row snippets with `DL = gap - 1`
holds. The constraint is in `slice_voice_phrases`, which collapses
each phrase to a single delta at `rel=0`.

Lift: walk each phrase's per-frame state and emit a new delta entry
whenever the (`WGh`, `AF`) tuple changes within the phrase. Other
columns (`AD`/`SR`/`PW`) stay at phrase-start values to keep snippet
row counts bounded.

### 2. Phrase boundary on note-change only

Drop gate-bit toggle from `slice_voice_phrases`'s boundary
criterion. A phrase now runs from one note byte to the next; gate
transitions are encoded as `WGh` column flips within the phrase's
multi-row snippet.

This is a semantic shift. Today a phrase = "one held note + one
gate state". After: a phrase = "one held note byte across any gate
transitions until the note byte changes".

### 3. `GATE_A`-only pattern events for non-re-gating triggers

`_build_voice_chunks` currently fires `FLAG_GATE_N | FLAG_GATE_A`
on every phrase trigger. `FLAG_GATE_N` resets `slide_mode` +
`slide_accum` per player line 990. After change 2, every phrase IS
a fresh note → `GATE_N` is correct on phrase triggers. No new event
type needed for the slide path itself.

(Open question: are there note-byte transitions in the source where
the slide accumulator should carry forward across? Current data
says no — the next gate-on at frame 396 lands on a new on-LUT freq,
which means a fresh `GATE_N` is fine.)

### 4. Slide-mode reverse-mapping

Active slides only ($80..$FF range). Single-frame delta is fixed
per `slide_mode`:

```python
SLIDE_DELTAS: dict[int, int] = {}   # slide_mode -> signed 16-bit delta
for S in range(0x80, 0x100):
    y = (S << 1) & 0xFF
    if y & 0x80:
        d = -((_lut(SLIDE_DEC_HI_BASE, y) << 8) | _lut(SLIDE_DEC_LO_BASE, y))
    else:
        d = (_lut(SLIDE_INC_HI_BASE, y) << 8) | _lut(SLIDE_INC_LO_BASE, y)
    SLIDE_DELTAS[S] = d
```

For each phrase, find the first frame `T` where observed freq
diverges from `NOTE_PITCH[note]`. Compute `delta = obs[T] -
NOTE_PITCH[note]` (signed 16-bit). For `k > T`, verify `obs[k] -
NOTE_PITCH[note] == (k - T + 1) * delta` (allows constant-rate
slides only; portamento and non-linear sweeps are out of scope).
Match `delta` exactly against `SLIDE_DELTAS`; emit `AF` column with
that `slide_mode` in a row at rel=T.

Pitch-base subtlety: during active slide, the player emits `freq =
accum + NOTE_PITCH[note]` (NO `pitch_base` offset, player lines
796-801). For V0 (`pitch_base=0`) this matches LUT directly; for
V1/V2 (`pitch_base=1/2`) the first slide frame is off-LUT in the
per-voice LUT. To detect slides on V1/V2, the slide-detection step
walks raw `(freq_lo, freq_hi)` per frame and compares to
`NOTE_PITCH[note]` (no pitch_base) — independent of `snap_note`'s
LUT lookup. Phrase note assignment stays correct because note is
established at gate-on before slide engages (frame 384 in the
example shows the unsubstituted NOTE_PITCH for two frames).

## Algorithm

```
for each phrase P (after slice_voice_phrases relaxation):
    note = P.note
    base = (NOTE_PITCH_HI[note] << 8) | NOTE_PITCH_LO[note]
    drifts = [signed16(obs_freq[k] - base) for k in 0..P.length-1]
    
    # Find slide-start frame T: first k where drift[k] != 0 and
    # drift[k:] forms a linear ramp.
    T = first_k_with_nonzero_drift(drifts)
    if T is None:
        continue  # no slide
    
    delta = drifts[T]
    if delta == 0:
        continue
    
    # Verify linearity: drift[T+j] == (j+1) * delta for all j in range.
    if not all(drifts[T+j] == (j+1) * delta for j in 0..len(drifts)-T-1):
        continue  # nonlinear; out of scope
    
    # Find slide_mode S whose precomputed delta matches.
    S = SLIDE_DELTAS_INVERSE.get(delta)
    if S is None:
        continue
    
    # Emit a second delta entry at rel=T with AF column.
    # WGh column at rel=T may also flip if the gate-bit changes at that frame.
    P.deltas.append((T, build_cols_at_frame(P, T, af=S)))
```

The phrase's clustering key now reflects multi-row content, so
slide phrases get their own cluster (won't share with non-slide
phrases of the same gate-on patch).

## Expected wins / sidTAB-row cost

Measured via `profile/glow_worm_slide_audit.py`
(2026-05-17). Foundational model validated by
`tests/test_defmon_slides_model.py` (8 tests + 37 subtests on
synthetic .prgs).

**Phrase / hit tally (note-change-only boundary):**

| Voice | Total phrases | No-slide | Off-LUT | Linear-fit + S match | Off-LUT non-linear |
|---|---|---|---|---|---|
| V0 | 192 | 100 | 92 | **74** | 18 |
| V1 | 346 | 327 | 19 | 0 | 19 |
| V2 | 349 | 310 | 39 | 4 | 35 |
| **total** | **887** | **737** | **150** | **78** | **72** |

**Two findings rewrite the design's expectations:**

1. **Glow_worm uses exactly two slide_mode bytes** across all 78 hits:
   - `$E0` (delta = -351): 48 hits — V0's "release slide" pattern.
   - `$B0` (delta = +2228): 30 hits — V0 + V2 upward sweeps.
   
   The reverse-mapping needs to match exactly these two values; the
   remaining 126 modes in `SLIDE_DELTAS` are inert on this corpus.
   The 4 V2 hits are also `$B0`.

2. **T_offset distribution is concentrated**: V0 hits split between
   T=2 (48 phrases) and T=4 (26 phrases). Combined with the two
   slide_modes, glow_worm has only **9 distinct
   `(WGh, AD, SR, PW, AF, T)` cluster keys** across all voices
   (7 on V0, 2 on V2, 0 on V1).

**sidTAB-row cost: trivially small.**

- Multi-row instruments: each slide cluster = 2 rows (row 0 with
  WGh/AD/SR/PW; row 1 with AF after a T-frame hold).
- Cross-voice merge (already landed in `0ea201a`) dedupes shared
  cluster keys.
- Glow_worm: 9 cluster keys × 2 rows = **18 sidTAB rows added**.
  After cross-voice merge, possibly less.
- Current rows used: 231/256. After: ~245-249/256. **Within budget
  by a wide margin** — the bloat-relief commit was sufficient.

The original design's "240..320 rows, sidTAB blows capacity" risk
is **refuted**. JP tail-merge isn't needed for glow_worm.

**Recall lift target (measured ceiling):**

- V0 slide-span × 2 regs/frame (max CSV rows recoverable): 1076
- V2 slide-span × 2 regs/frame: 28
- **Total slide hits cover ~1104 CSV rows max**, i.e. the lift if
  every per-frame freq write in a slide-hit phrase reverse-maps
  cleanly.
- Realistic lift: lower (some writes are dedupe-suppressed when
  freq_lo or freq_hi stays unchanged across a frame; CSV reduces
  multi-write frames to single writes per (reg, frame)).

**Projected post-slides recall on glow_worm:**

| metric | baseline | after slides (max) |
|---|---|---|
| overall recall | 832/14236 = 5.84% | ~1900/14236 ≈ **13%** |
| V0.freq recall | 97/2239 = 4.33% | ~1173/2239 ≈ **52%** |
| V2.freq recall | 242/1451 = 16.68% | ~270/1451 ≈ 19% |
| V1.freq recall | 119/909 = 13.09% | unchanged (0 slide hits) |

Roughly **2.3× overall lift**, dominated by V0. Smaller than the
original "+1500..+2500" estimate.

## Test plan

**Foundational tests already landed** (`tests/test_defmon_slides_model.py`):
8 tests + 37 subtests using synthetic .prgs built directly from
`DefmonSong` + run through `DefmonPlayer`. Cover: no-slide baseline
across all voices, active slide $80..$FF linear progression
(matches `predicted_slide_delta` LUT formula, mod 16-bit wrap),
zero-rate boundary modes, multi-row DL hold delay, GATE_N reset of
slide state, gate-off via WGh preserves slide_mode, SLIDE_DELTAS
symmetry. If any assertion here regresses post-impl, the slide
reverse-mapping is wrong.

**Refinement audit landed** (`profile/glow_worm_slide_audit.py`):
the measurements that produced the "Expected wins" table above.
Re-runs on demand if data changes.

**Post-impl gates** (`tests/test_dump_to_defmon_round_trip.py`
baseline bump on land):
- `overall_matched` ≥ new floor (target ~1800+ matched, up from 832).
- V0.freq recall ≥ new floor (target ~45%+, up from 4.33%).
- sidTAB rows used ≤ 256 (don't blow capacity — audit says ~245).
- per-register-class recall floor on V1.freq + V2.freq doesn't
  regress (slide impl shouldn't perturb non-slide phrases).

Manual ear audit on regenerated WAV.

## Risks

- **Non-linear off-LUT trajectories** dominate V1/V2 (54/58 off-LUT
  phrases). Audit sampling shows these are **vibrato**: an initial
  large step then ±22-magnitude oscillation per frame. Could be
  TR-column-driven multi-rate cycling or PS-pulse-width side-
  effects. **Out of scope** for the slides MVP. Mitigation:
  detection is conservative (exact integer match against
  `SLIDE_DELTAS`); non-linear trajectories fall back to
  `snap_note prev_note`. Future: a separate "vibrato modeling"
  follow-up could revisit these, but that's a different reverse-
  mapping problem.
- **~~Multi-row blows sidTAB capacity~~**: REFUTED by audit
  (9 cluster keys → +18 rows max → 245-249/256). JP tail-merge
  unnecessary for glow_worm.
- **Voice-1/2 pitch-base discrepancy** in slides where `AF` is set
  at gate-on. Audit confirms V1 has zero such phrases on glow_worm;
  V2 has 4. Mitigation: detection treats `NOTE_PITCH[note]` as the
  slide baseline (no pitch_base, per foundational test
  `TestActiveSlideLinear`).
- **Phrase boundary relaxation changes recall non-monotonically**
  on register classes other than freq. The cross-voice merge from
  the prior commit (`0ea201a`) was byte-neutral; this change isn't.
  Mitigation: per-register-class recall in the harness output makes
  regressions visible.
- **`DefmonSong.clear_song_table()` library gotcha**: wipes
  $1B00..$1EFF (the entire `voice_pattern_refs` 0x400-byte
  property), which INCLUDES the DL region at $1E00..$1EFF.
  `build_song` already orders `clear_song_table()` before any
  `set_dl` so DL writes survive; the slides impl must preserve
  that ordering, and the synthetic-prg test helper documents the
  gotcha (`tests/test_defmon_slides_model.py:16-21`). Library fix
  is out of scope for this design.

## Out of scope

- PS pulse-width sweeps (backlog item #3) — separate reverse-mapping.
- ACID cutoff slides (backlog item #1) — global, not per-voice sidTAB.
- RE column bitmask path (backlog item #4) — small win.
- JP tail-merge for multi-row instruments — wait for first impl to
  show whether budget pressure actually demands it.
- Portamento slides (slide_mode in $01..$7F).

## Followups discovered post-implementation (2026-05-17)

### Backlog item retest results

After landing slides + PS + gate-bit boundary + trigger-placement
fix (recall 2074/14236 = 14.57%):

- **rel=1 underfill (item #2)** — tried emitting the trigger 1 frame
  early to bridge the gap=1 unfillable case. Regressed overall recall
  from 2074 → 1967 (−107): the 1-frame shift breaks CSV alignment for
  the affected phrase's WGh/AD/SR/PW/note writes faster than it adds
  matches. Reverted; the drop comment in `_build_voice_chunks`
  documents the negative outcome. Not a clean win without a wider
  trigger-placement model.

- **ACID cutoff (item #1)** — inspected glow_worm.prg's actual sidTAB:
  ACID lives at rows 44/45/46 (`ACID=$4000` absolute set, then
  `ACID=$C200` SBC step=2 with DL=18, then `ACID=$8200` ADC step=2
  with DL=18) and is gated via `slot_b` (layer 1) in patterns 4/5/6 at
  event 1. The cutoff trajectory is a 38-frame triangle driven by the
  cascade walking that 3-row sequence in layer 1.
  Forward simulation of the player's `_cutoff_slide_step` confirms
  the absolute-set + SBC-step-2 setup reproduces the first 20-frame
  descent exactly (predicted 2,64,62,…,26 == observed). After 20
  frames the predicted trajectory clamps to floor=2 while observed
  reverses up — the reversal is driven by row 46's ADC command
  landing via the layer-1 cascade.
  **Blocker:** our `dump_to_defmon` only emits `slot_a` (layer 0).
  Reverse-mapping ACID requires building a layer-1 cascade
  driver — a separate channel of sidTAB rows + GATE_B emission +
  cluster keying that includes layer-1 phase. This is multi-day
  architectural work, not a surgical fix.

- **RE bitmask (item #4)** — 41 RE writes in glow_worm; 26/41
  coincide with phrase trigger frames, 15/41 fall between triggers
  (cascade-driven). The bitmask path `(prev & 0x0F) | val | voice_or`
  cannot produce the observed RE values ($F8, $FA, $FC, $FE) from any
  voice via the bit-3-set path — the OR'd `voice_or` bit always sets
  the low bit. The values must come from the `val=0` (clear) path
  with a specific prior state, or from explicit `$D417` writes in
  song-init code. Either way, like ACID, requires layer-1 cascade
  infrastructure to emit the actual byte sequence. **Blocker:** same
  as ACID — layer-1.

- **Multi-row instruments for non-slide phrases (item #5)** — without
  a driving use case (ACID/RE both need layer-1, not extra rows on
  layer 0), this gives no immediate recall win on its own. Defer
  until layer-1 lands or a phrase-trajectory analysis surfaces a
  layer-0 multi-row pattern.

### Next architectural step

A layer-1 cascade pass is the gating dependency for both ACID
(~+1500 potential matches) and RE (~+15 conservative). Design
doc: [`dump_to_defmon_layer1_design.md`](dump_to_defmon_layer1_design.md).

## Rollout

One commit landing changes 1+2+3+4 together. The CSV-recall harness
+ test pin (`0ea201a`) is the regression gate; any change that
breaks recall below the new floor reverts. Baseline fixtures
regenerated and bumped in the same commit. Update AGENTS.md
"Resolved" with the verdict numbers, drop backlog item #2.
