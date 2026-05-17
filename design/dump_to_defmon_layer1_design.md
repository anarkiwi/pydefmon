# dump_to_defmon layer-1 cascade — design note

## What

Extend `pydefmon/dump_to_defmon.py` to emit defMON's
**layer-1 sidcall cascade** in addition to the layer-0 cascade it
emits today. Layer-1 is the second cascade channel (`slot_b` +
`FLAG_GATE_B`) the player walks in parallel with layer-0; it
carries global side effects (cutoff drive, RE/CP/FV routing) and
per-voice multi-row transients that the current single-channel
encoder cannot reproduce.

## Why

Per-register recall on glow_worm with the current single-layer
encoder (commit `477dd94`, 2074/14236 = 14.57%):

| class | matched | source | recall |
|---|---|---|---|
| cutoff (`$D416`) | 1 | 1595 | 0.06% |
| res (`$D417`) | 0 | 41 | 0% |

The 2026-05-17 audit of `glow_worm.prg` (recorded in
[`dump_to_defmon_slides_design.md`](dump_to_defmon_slides_design.md)
§"Followups discovered post-implementation") established that:

* glow_worm's cutoff trajectory is driven by a 3-row layer-1
  cascade at sidTAB rows 44/45/46 (`ACID=$4000` absolute set,
  `ACID=$C200` SBC slide DL=18, `ACID=$8200` ADC slide DL=18).
* Forward simulation of the player's `_cutoff_slide_step` from
  these rows reproduces the first 20-frame descent of the cutoff
  emit byte-for-byte; the periodic reversal is the row-46 ADC
  command being reached via the layer-1 cascade.
* Layer-1 is re-armed via `FLAG_GATE_B` pattern events; glow_worm
  has 110 such events across 30 patterns. Per-voice usage:

  | voice | unique patterns | GATE_B events | target rows |
  |---|---|---|---|
  | V0 | 3 | 3 | 21 |
  | V1 | 13 | 71 | 0×69, 39, 44 |
  | V2 | 14 | 38 | 0×17, 1, 2×5, 7, 21, 39×4, 42, 44×8 |

  V2 is the primary ACID driver (8 of 9 `slot_b=44` re-arms);
  V1 carries 69 layer-1-stop events (`slot_b=0` with `FLAG_GATE_B`).
* RE writes (`$D417`) at frames where no voice triggers come from
  the same layer-1 mechanism (cascade walks reach an `RE`-bearing
  row). Of 41 RE writes, 26 coincide with a voice trigger frame
  and 15 fall in cascade-driven gaps. The bitmask path
  `(prev & 0x0F) | val | voice_or` cannot produce the observed
  values ($F8/$FA/$FC/$FE) from any voice via the bit-3-set path,
  confirming the writes are not from per-voice gate-on rows.

**Expected lift on glow_worm:**

| metric | now | after layer-1 (max) |
|---|---|---|
| overall recall | 2074/14236 = 14.6% | ~3700/14236 ≈ **26%** |
| cutoff recall | 1/1595 = 0.06% | ~1500/1595 ≈ 94% |
| res recall | 0/41 = 0% | ~30/41 ≈ 73% |

Roughly **+1600–1800 matches**, dominated by cutoff. RE is small
in absolute terms but high-percentage. Multi-row layer-1 effects
(TR transients at row 39, AD/SR row 21) may also nudge V0/V1/V2
ctrl + ad + sr recall, but those gains are bonus, not the load-
bearing case.

## Scope

In:

1. A `Layer1Driver` pass that profiles the source CSV's
   `$D416`/`$D417`/`$D418`/`$D415` write stream, segments it into
   "layer-1 epochs" (windows where one layer-1 cascade walk
   explains the trajectory), and emits the layer-1 sidTAB row
   sequence + DL spacing for each epoch.
2. A `Layer1Instrument` cluster keyed on `(rows_tuple, dl_tuple)`
   so identical epochs share rows (analogous to the existing
   layer-0 `Instrument` dedupe).
3. Layer-1 emission in `_build_voice_chunks`:
   * `slot_b` set to the layer-1 instrument's start row on every
     layer-0 trigger event that needs the layer-1 state held
     (carried, not gated).
   * `FLAG_GATE_B` set on the pattern event that begins each
     layer-1 epoch (re-arms the layer-1 cascade).
   * Layer-1 stop events (`slot_b=0` + `FLAG_GATE_B`) emitted at
     the frame where the source CSV's layer-1 trajectory ends.
4. Voice choice for `GATE_B` emission: pick the voice whose layer-0
   trigger frame is closest to the layer-1 epoch boundary, with
   `slot_b=0` carry-through on the other two voices to avoid
   accidental re-arms.
5. Forward-simulator helper for the cutoff slide (already inline in
   `defmon_player._cutoff_slide_step`; exposed via a thin wrapper
   that takes `(acc_lo, acc_hi, step_lo, step_hi, op, extra, floor,
   asl)` and returns the next emit byte). Used to verify each
   candidate layer-1 instrument before committing it.

Out:

* Layer-1-driven AF (slide_mode) transients — current slide impl
  is layer-0 multi-row. Layer-1 AF would be a follow-up if the
  trajectory analysis surfaces it.
* RE bitmask reverse-mapping beyond what layer-1 cascades produce
  (e.g. if a row's RE column would need a different byte than the
  one the bitmask logic produces). Conservative: only emit RE
  values that the cascade-path forward computation matches.
* `$D415` (cutoff low) — observed empty in glow_worm; if other
  tunes have writes here, defer to a follow-up.
* JP-target rewriting for shared layer-1 tails (layer-0's
  tail-merge is still out of scope per slides design doc).

## How

### Pipeline shape

Replay-pipeline addition between current stages B (phrase slicing)
and C (cluster phrases):

```
A.  replay_csv          -- per-frame target state (unchanged)
B.  slice_voice_phrases -- per-voice phrases (unchanged)
B'. _merge_slide_continuations / _detect_phrase_ps (unchanged)

[NEW]
B*. profile_layer1      -- scan FrameState.cp / re / fv / cutoff
                           emit-trajectory; identify layer-1
                           epochs; emit Layer1Instrument list +
                           per-frame attach metadata
[/NEW]

C.  cluster_phrases     -- layer-0 instruments (unchanged)
D.  lay_snippets        -- now lays layer-0 AND layer-1 rows in the
                           same sidTAB region (cluster IDs distinct)
F.  build_chunked_patterns
                        -- now writes slot_b / FLAG_GATE_B per the
                           layer-1 attach metadata
G.  serialize song
```

`Layer1Instrument` (frozen dataclass):

```python
@dataclass(frozen=True)
class Layer1Instrument:
    rows: tuple[tuple[tuple[str, int], ...], ...]  # ((cols),...)
    dls:  tuple[int, ...]                          # one per row
    jps:  tuple[int, ...]                          # one per row
```

Each `rows[i]` is the column-set for layer-1 row `i`; `dls[i]` is
that row's DL; `jps[i]` is the JP target (or `-1` for active
linear). Final row is STop (`dl=$80`) unless the trajectory needs
explicit jump-back-to-row-0 looping.

### Layer-1 epoch detection — cutoff

Walk `states[].cutoff_emit_predicted`, where the predicted emit is
the forward-simulation result of feeding the player's
`_cutoff_slide_step` from a candidate state. The detection loop:

1. At each frame F, if predicted emit ≠ observed emit, this is an
   epoch boundary — the source CSV must have applied a new ACID
   command here (cascade reached a new layer-1 row).
2. Search the small space of ACID commands that, when applied at F,
   make subsequent predictions agree with observed for the next ≥ K
   frames (K starts at 4, grows on success).
3. Two ACID shapes to enumerate:
   * **Absolute set** (high bit 7 = 0): `(acc_hi, acc_lo)` ∈ {0..$7F}
     × {0..$FF}, with `acc_hi` masked to the floor of the *next*
     observed emit byte minus extra/carry contribution. Brute-force
     ~32k combos at ~µs each is acceptable; in practice the next
     observed emit + the player's clamp logic narrows to a handful.
   * **Slide** (high bit 7 = 1): `op ∈ {ADC, SBC}`, `step_hi ∈ {0..$3F}`,
     `step_lo ∈ {0..$FF}`. Enumerate; pick the first match.
4. The first match wins; continue forward sim from the new state.

For glow_worm, the epoch sequence is:

```
F=0       : absolute set acc_hi=$40, acc_lo=$00      (row 44 equiv)
F=1       : SBC slide step_hi=2, step_lo=0           (row 45 equiv)
F=20..21  : ADC slide step_hi=2, step_lo=0           (row 46 equiv)
F=39..40  : SBC slide step_hi=2, step_lo=0           (cycle)
... repeats with periodic absolute-set "resets"
```

The detector outputs one **command sequence** per layer-1 epoch.
A `Layer1Instrument` packs each sequence as a row-walk with DL
gaps matching the inter-command frame deltas.

### Layer-1 epoch detection — RE / FV / CP

Same shape, simpler: each `$D417` / `$D418` / `$D415` write that
the per-voice GATE_A layer-0 row apply cannot produce becomes a
layer-1 row apply. The "what byte to write" is direct (the
observed value), modulated by the bitmask logic if RE bit 3 is
set — but for trajectories where the bitmask path is impossible
(see glow_worm RE values), the layer-1 row's RE byte must be the
final value via the `val == 0` clear-path or `else` direct-set
branch.

### Attach metadata + voice choice

For each layer-1 epoch with start frame `Fs` and end frame `Fe`,
the planner emits:

* `GATE_B` event at the layer-0 pattern slot closest to `Fs`. If
  no layer-0 trigger lands within ≤2 frames of `Fs`, insert a
  GATE-B-only pattern event (slot_a=0, no FLAG_GATE_A, just
  FLAG_GATE_B + slot_b = epoch start row).
* `slot_b` carried (no GATE_B) on every layer-0 trigger event in
  `[Fs, Fe)` so the cascade keeps walking without re-arming. This
  matches glow_worm's pattern: 3364 events have non-zero slot_b
  but only 110 fire `FLAG_GATE_B`.
* Stop event (`slot_b=0` + `FLAG_GATE_B`) at the nearest layer-0
  trigger past `Fe`. If no nearby trigger exists, accept slight
  cascade overrun — the layer-1 row at `Fe`'s landing row should
  carry `dl=$80` STop to freeze the cascade naturally.

Voice choice prefers V2 (matches glow_worm; V2's high trigger
density gives the planner flexibility). Fallback to V1 then V0.

### Cluster keying changes

Layer-0 `Instrument` cluster key today is `(rel, cols_tuple)` over
all phrase deltas. Layer-1 is independent; the layer-0 cluster
stays the same. But two layer-0 phrases that today cluster together
will diverge if they're attached to different layer-1 epochs (one
needs `slot_b=44` carried, the other `slot_b=0`). That doesn't
require splitting layer-0 clusters — the `slot_b` field lives on
the pattern event, not the layer-0 sidTAB row.

### sidTAB capacity

Current usage: 237/256 rows on glow_worm. Layer-1 epochs add rows.
Conservative estimate from the audit:

* 3-row ACID instrument (rows 44/45/46 equivalent): 3 rows
* 2-row TR transient (row 39/40 equivalent): 2 rows
* 1-row AD/SR effect (row 21 equivalent): 1 row
* 1-row WGl effect (row 42 equivalent): 1 row
* 1-row global-init (row 7-equivalent with `RE=0, FV=16, CP=0,
  ACID=0`): 1 row

Total: ~8 new layer-1 rows for glow_worm. Headroom: 19 rows.
Comfortable for the prototype. Will need monitoring on more
complex tunes.

## Test plan

**Foundational synthetic-prg tests** (new file
`tests/test_defmon_layer1_model.py`):

1. **No layer-1 baseline**: empty layer-1 sidTAB region; the
   player's cutoff stays at floor=2 for the song duration.
2. **Absolute-set ACID**: row N with `ACID=$4000`, DL=$80 STop;
   trigger via `FLAG_GATE_B`, slot_b=N. Verify `$D416` emit is
   $40 starting at frame N+1 (with the +carry offset from the
   first slide step).
3. **SBC slide step=2**: rows N (abs $4000, DL=0) → N+1 (slide
   $C200, DL=$80 STop). Verify 19-frame linear descent
   64→62→60→…→26 matches the existing forward-simulator output.
4. **ADC reversal**: rows N (abs) → N+1 (SBC, DL=18) → N+2 (ADC,
   DL=18). Verify the trajectory hits floor and reverses; assert
   the +1/-1 transient frames at slope-change boundaries.
5. **GATE_B re-arm mid-cascade**: layer-1 cascade on row N+1
   (mid-SBC). Mid-walk, fire `FLAG_GATE_B` + slot_b=N to restart.
   Verify the cascade resets to row N's state.
6. **`slot_b=0` stop with GATE_B**: layer-1 cascade walking,
   then a pattern event with `slot_b=0, FLAG_GATE_B`. Verify
   subsequent frames freeze the layer-1 state (no further row
   applies).
7. **Layer-0 and layer-1 cohabitation**: layer-0 GATE_A walks a
   pitch slide while layer-1 walks an ACID slide. Verify both
   trajectories reproduce independently.
8. **RE bitmask via row apply**: row N with `RE=$F8`, layer-1
   cascade arms at N from V2. Verify the resulting `$D417` emit
   matches the player's bitmask computation (this test will
   surface whether glow_worm-style $F8 values are reachable;
   may need to test alternative voices or `val=0` plus
   pre-state).

**Refinement audit** (new file
`profile/glow_worm_layer1_audit.py`): scan the
source CSV, run the cutoff/RE/FV epoch detector, and report:

* number of layer-1 epochs found,
* sidTAB row count per epoch shape,
* per-frame predicted-vs-observed cutoff diff (for the audit's
  acceptance gate),
* GATE_B event count per voice the planner would emit.

Re-run on demand.

**Post-impl gates** (`tests/test_dump_to_defmon_round_trip.py`
baseline bump on land):

* `overall_matched` ≥ new floor (target ≥ 3500, up from 2074).
* `cutoff` recall ≥ 80% (≥ 1280/1595).
* `res` recall ≥ 50% (≥ 20/41).
* sidTAB rows used ≤ 252 (≥ 4-row headroom for follow-ups).
* No regression on V0.freq, V0.ctrl, V2.freq, V2.pw recall.

Manual ear audit on regenerated WAV (per project policy:
[[feedback_no_audio_compare_tracker_export]]).

## Risks

* **Forward-sim drift across `sub_frame_count` transitions.** The
  cutoff emit timing differs between N=1 (one NMI per frame) and
  N≥2 (emit-shift). glow_worm runs at N=1. If other tunes use
  N≥2, the layer-1 detector needs the N-aware simulator.
  Mitigation: detector takes `sub_frame_count` from the CSV's IRQ
  rate hint or defaults to 1; cross-tune validation deferred to
  follow-up.
* **Voice-choice misses an `Fs` epoch boundary** by > 2 frames
  with no nearby trigger. Falls through to an insert-GATE-B-only
  event. That event has no GATE_A (so layer-0 cascade unchanged)
  and `note=0`. Cost: one pattern slot per such epoch. Mitigation:
  budget-check the pattern table after planning; warn if it
  exceeds 120/128.
* **Layer-1 cascade collisions on `slot_b=0` carry-through.** If
  voice V's pattern event has `slot_b=0` without `FLAG_GATE_B`,
  the player's existing layer-1 cascade keeps walking — no harm.
  But if a different voice on the same frame fires `FLAG_GATE_B`
  with `slot_b=44`, the cascade re-arms. Per-voice cascade state:
  layer-1 is voice-local. Each voice has its own layer-1
  accumulator. Mitigation: design assumes per-voice; verify in
  foundational test 5 + 7.
* **ACID epoch detector enumeration cost.** Brute-forcing ~32k
  absolute-set combos at every potential epoch start can be
  expensive on long CSVs. Mitigation: prune by observed-emit
  constraint (acc_hi ≈ observed - extra - carry); typical search
  reduces to single-digit candidates.
* **Cluster-key explosion.** The slot_b field on pattern events
  may diverge across phrases that previously shared a body via
  the body-dedup pass in `build_song` (line 781-799). Patterns
  with different slot_b sequences are distinct bodies. Mitigation:
  measure pattern-count growth in the audit; budget-check in the
  post-impl gate.
* **`DefmonSong.clear_song_table()` interaction.** Same gotcha
  documented in slides design — call before any `set_dl`.
  Reaffirm in the new code path.

## Out of scope

* Layer-1 AF (slide_mode) effects (could be a follow-up).
* Multi-voice layer-1 sharing (one layer-1 cascade re-armed
  simultaneously across voices). Per-voice independent layer-1
  is sufficient for glow_worm; collapse to shared if a future
  tune demands it.
* JP tail-merge for layer-1 rows.

## Rollout

Three-step rollout per
[[feedback_design_first]] (separate commits so each step can land
or revert independently):

1. **Foundational tests + audit harness** (commit 1):
   `tests/test_defmon_layer1_model.py` + the glow_worm layer-1
   audit script. Tests validate the player's layer-1 behaviour;
   audit reports planner expectations. No production code change.
2. **Forward simulator + epoch detector + Layer1Instrument**
   (commit 2): `pydefmon/dump_to_defmon.py` gains the
   Stage B* profiler + `Layer1Instrument` cluster + lay_snippets
   extension. Build-song wiring kept disabled behind a default-off
   flag; tests confirm the planner output matches the audit.
3. **Wire layer-1 into build_song + bump pin** (commit 3):
   `_build_voice_chunks` learns to emit `slot_b` / `FLAG_GATE_B`;
   round-trip test pin bumped; baseline fixtures regenerated;
   AGENTS.md updated.

Manual ear audit on the regenerated WAV at each gate. CSV-recall
remains the regression gate (per
[[feedback_no_audio_compare_tracker_export]]).

## References

* Player layer-1 cascade: `pydefmon/defmon_player.py`
  `$1738` bitmask path, `$1764` CP store, `$176D` ACID dispatch.
* ACID slide oscillator: `pydefmon/defmon_player.py`
  `_cutoff_slide_step` (lines 644-694).
* Slides design (predecessor):
  [`dump_to_defmon_slides_design.md`](dump_to_defmon_slides_design.md).
* glow_worm audit data: this doc §"Why", measurements from
  `fixtures/glow_worm.{csv,prg}`,
  2026-05-17.
