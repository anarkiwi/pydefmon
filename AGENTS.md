# AGENTS.md — pydefmon

Operating notes for Claude / coding agents working in this repo.

## What this repo is

A Python implementation of the **defMON** C64 tracker's runtime
behaviour. Three subsystems:

1. **On-disk format** (`pydefmon/defmon.py`). `DefmonSong` /
   `PatternEvent` / `SidtabRow` data classes; `_load_format.py`
   round-trips the `$D6C9` load-time codec byte-for-byte against the
   real defMON loader.
2. **Player** (`pydefmon/defmon_player.py`). Frame-accurate
   register-level emulation of the `$0AED` NMI handler: sidcall
   cascade (two layers per voice), pitch slide ($1405), PS
   pulse-width sweep ($1495), and ACID cutoff slide ($10B5-$10D7).
   Emits `(reg, value)` SID writes per main frame; `render_wav`
   uses `pyresidfp` to render audio.
3. **Reconstructor** (`pydefmon/dump_to_defmon.py`). Given a
   `defmon_player` CSV write log, reverse-maps observed register
   trajectories to a defMON .prg that re-renders through the player
   with matching frame-by-frame state. Used as the encoder side of a
   round-trip fidelity check.

The canonical test fixture is **`fixtures/glow_worm.{csv,prg}`** — the
[Glow Worm](https://csdb.dk/sid/?id=63421) demotune by Tomek Grynfelder.
All recall figures below are measured on this fixture.

## Current state (2026-05-17)

### Reconstructor recall progression

`tests/test_dump_to_defmon_round_trip.py` pins the round-trip on
glow_worm. Round-trip recall is the count of exact `(frame, reg, value)`
matches between the source CSV and a re-rendered recon CSV.

| stage                                      | matched / 14236 | overall |
|--------------------------------------------|----------------|---------|
| initial snippet+JP MVP                     | 832            | 5.8%    |
| cross-voice merge + pattern dedup          | 832            | 5.8%    |
| slide reverse-mapping (note-only boundary) | 1079           | 7.6%    |
| option B (gate-bit boundary + slide-merge) | 1215           | 8.5%    |
| PS sweep reverse-mapping                   | 1468           | 10.3%   |
| trigger-placement fix (`dur=1` on gap=3)   | 2074           | 14.6%   |
| **layer-1 ACID cascade (current)**         | **2586**       | **18.2%** |

Per-class recall at the current pin (`fixtures/glow_worm.recon.baseline.csv`):

| class    | matched / total |        |
|----------|----------------:|-------:|
| V0.freq  | 483 / 2239      | 21.6%  |
| V0.ctrl  | 134 / 776       | 17.3%  |
| V2.freq  | 500 / 1451      | 34.5%  |
| V2.pw    | 499 / 3715      | 13.4%  |
| cutoff   | 513 / 1595      | 32.2%  |
| res      | 0 / 41          | 0%     |
| volume   | 1 / 5           | 20%    |

The **layer-1 cascade** lays a 4-row JP-loop ACID drive
(`abs $0040 / SBC $00C2 DL=18 / ADC $0082 DL=18 / JP back to SBC`)
at sidTAB rows 237-240 and re-arms it via `FLAG_GATE_B + slot_b` on
each detected reset-frame trigger (7/16 attached, exact-frame match).

### Audit-discovered ceiling

The cutoff recall is bottle-necked by **arranger drift**, not the
nine unattached reset frames. Audit details:

* `build_song`'s `_build_voice_chunks` allows `slack ∈ {0, 1}` with
  `alt_dur = max(0, slack)`. Slack=1 (= last trigger ends at
  `cur_rel=61`) lengthens that chunk by one frame.
* Drift accumulates across chunks. Empirically (recon traced via
  player), chunks 12-16 vary 62-65 frames; by chunk 25 the actual
  start frame has drifted by 61 frames vs the naive `N*63`.
* As a result, post-drift triggers fire at the wrong source frame.
  Layer-1 windows after the first attached fire produce ≤6 matches
  each (vs hundreds in the first window).

Fixing the slack so every chunk is exactly 63 frames should
materially lift recall across all register classes, not just cutoff.

## Test pins

* `tests/test_defmon.py` — DefmonSong / PatternEvent / SidtabRow
  data-class API.
* `tests/test_defmon_player_portamento.py` — pitch slide ($1405)
  against synthetic .prgs.
* `tests/test_defmon_slides_model.py` — 12 tests, foundational
  pitch-slide + PS oscillator behaviour.
* `tests/test_defmon_layer1_model.py` — 14 tests, foundational
  layer-1 cascade behaviour (ACID abs/SBC/ADC, GATE_B re-arm,
  slot_b=0 stop, RE bitmask). Includes
  `TestForwardSimulatorMatchesPlayer` cross-checks of
  `CutoffSlideState` against the player's `_cutoff_slide_step`.
* `tests/test_dump_to_defmon_round_trip.py` — bytewise +
  recall-count pin on glow_worm.

## Forward-looking work

### Next architectural step

**Fix arranger slack drift.** Two options outlined in chat / design
notes:

* **A. Last-trigger dur absorb.** When `slack=1`, bump the last
  trigger's `dur` from 0 to 1 so cur_rel lands exactly on 62.
  Surgical (~10 lines).
* **B. Full slack-0 audit.** Restructure `_build_voice_chunks` to
  enforce exact 63-frame chunks across all configurations
  (re-balance delays + trigger durs at chunk end).

Recommend A first; if recall jumps materially, the residual 62/65
chunks tell us whether B is needed.

### Remaining defmon-side recall work

After arranger-drift is fixed, the next targets (recall-ROI ordered):

1. Attach the 9 unattached layer-1 reset frames via insert-GATE-B-only
   events at trigger gaps (~+600-1000 matches).
2. **Multi-row instruments for non-slide phrases** — let phrase
   snippets cover intra-phrase deltas (WGh/AD/SR/PW changes mid-note).
   Bumps snippet row budget; tail-merge instruments via JP.
3. RE bitmask reverse-mapping (~+15 matches via layer-1; small but
   high-percentage on the 41-row class).
4. Phase-aware GATE_B re-arm pruning (some attached re-arms may be
   gratuitous resets mid-cycle).

## Design docs

* `design/dump_to_defmon_slides_design.md` — pitch slide reverse-map.
  Records the empirical refutation of "AF column at gate-on" and the
  T-internal slide-engagement model the impl uses.
* `design/dump_to_defmon_layer1_design.md` — layer-1 cascade
  infrastructure. Includes the glow_worm audit numbers and the
  three-step rollout this repo now reflects.

## Workflow conventions

* **Tracked infra, not untracked.** Audit scripts, designs, and
  fixtures live under tracked directories (`profile/`, `design/`,
  `fixtures/`).
* **CSV-recall is the regression gate.** Audio fidelity checks are
  human-in-the-loop ear audits on the WAV produced by
  `tools/run_dump_to_defmon_round_trip.sh`. Numerous prior contexts
  failed to converge on automated audio comparison; do not
  re-introduce one.
* **Design-doc-first for >1-day architectural work.** Land the doc
  with measurements, get sign-off, then implement in tracked commits
  that bump the test pin each time.
* **Re-present the TODO after committing a chunk.** Surface the
  forward-looking work for the next session.

## Library gotcha worth remembering

`DefmonSong.clear_song_table()` wipes `$1B00..$1EFF` — which INCLUDES
the DL region at `$1E00..$1EFF`. Always call it **before** any
`set_dl` writes. Reversing the order silently zeroes every DL byte
and the cascade refetches every frame instead of holding rows.
`_build_minimal_song` in the foundational tests documents this; the
production `dump_to_defmon.build_song` also follows the order.

## License

GPL-3.0-or-later (see `LICENSE`).
