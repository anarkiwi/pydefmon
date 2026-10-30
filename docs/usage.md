# Usage

Reading a tune and rendering per-frame SID writes is covered in the
[README](../README.md). This document covers editing and the command line. For
the file format and runtime RAM layout, see [format.md](format.md).

## Read, edit, write

```python
from pydefmon import DefmonSong, PatternEvent

# Read a .prg editor workfile or a .sid replay (auto-dispatch on container).
song = DefmonSong.from_file("tune.prg")
print(song.pattern_events(0)[:4])
print(song.sidtab_row(0))

# Structured edit API.
song.set_step(0, v1=1, v2=2, v3=3)        # arranger row 0 plays patterns 1/2/3
song.to_file("out.prg")
```

## Render per-frame SID writes

`DefmonPlayer` runs the replay embedded in a `.sid` (or a packed-form
`DefmonSong`). A `.prg` editor worktune carries no player: pack it first
with `DefmonSong.to_replay(editor_image)` — defMON's own SAVE pipeline
run on a decrunched editor image — then play the PSID (or use
`raw=True` for the packed `.prg`).

```python
from pysidtracker import song_frames
from pydefmon import DefmonPlayer, DefmonSong

player = DefmonPlayer("tune.sid")
for _ in range(song_frames("tune.sid")):
    writes = player.play_frame()           # (reg, value) list; reg is a 0..24 offset
    for reg, value in writes:
        ...

# Or a whole grid at once (nframes x 25 registers, forward-filled).
from pysidtracker import song_frames
grid = DefmonPlayer("tune.sid").render_grid(song_frames("tune.sid"))

# A .prg worktune, once packed with a user-supplied decrunched editor image.
song = DefmonSong.from_file("tune.prg")
psid = song.to_replay(editor_image)        # playable PSID bytes
prg = song.to_replay(editor_image, raw=True)
```

The player derives from `pysidtracker.MemPlayer`, so `play_frame`,
`render_grid`, and the register-log surface below all come from the shared base.

## Command line

pydefmon ships no CLI of its own: installing it registers defMON on the shared
`pysidtracker` tool (via the `pysidtracker.formats` entry point), which
recognises a defMON replay by content and drives it:

```bash
pysidtracker info   path/to/tune.sid                               # replay metadata
pysidtracker reglog path/to/tune.sid /tmp/tune.log  --seconds 60   # SID register log
pysidtracker wav    path/to/tune.sid /tmp/tune.wav  --seconds 60 --model 8580
```

`reglog` and `wav` render the tune's own replay for `--seconds` of playback.

## HVSC oracle survey

[`scripts/oracle_hvsc.py`](../scripts/oracle_hvsc.py) plugs `DefmonPlayer` into
the [`pysidtracker.survey`](https://github.com/anarkiwi/pysidtracker/blob/main/docs/survey.md)
whole-song comparison against the `sidtrace` oracle (Docker), over every subtune
of every HVSC defMON tune. Its `info` is the song's
[`variant`](format.md#variant-facets) facets (`container`, `variant` -- the decoded
replay layout, `editor` / `goto80` --, `signature`, `data_base` and the post-LOAD
`gate_*` bytes, each with a `<facet>.src` provenance string), plus `rsid`, the play
`trigger` (`pal_video` / `ntsc_video` / `cia_timer`), `speed` (play calls per PAL
frame) and `dynamic` (variable timer), the last four from
`pysidtracker.cadence.cadence_info`. `format:defmon` carries the same facets.

```bash
pip install "pysidtracker[survey]>=0.36.0"
python -m pysidtracker.survey find $HVSC --player '^defmon$' --out .hvsc-survey/tunes.csv
PYTHONPATH=scripts python -m pysidtracker.survey run $HVSC .hvsc-survey/tunes.csv \
    --work .hvsc-survey --render oracle_hvsc:render --jobs 32
python -m pysidtracker.survey triage .hvsc-survey
PYTHONPATH=scripts python -m pysidtracker.survey diff $HVSC .hvsc-survey/tunes.csv <md5>_<song> \
    --work .hvsc-survey --render oracle_hvsc:render
```

`.hvsc-survey/` holds copyright-derived traces and is gitignored.

### pathdiff strict xfails

[`tests/_support/pathdiff_xfails.json`](../tests/_support/pathdiff_xfails.json)
holds one case per fix in
[`pysidtracker.pathdiff`](https://github.com/anarkiwi/pysidtracker/blob/main/docs/pathdiff.md)'s
minimum cover of the survey's divergences, run as strict xfails by
[`tests/test_pathdiff_xfails.py`](../tests/test_pathdiff_xfails.py) in the `oracle`
job. An empty manifest means every surveyed subtune agrees. After a survey `run`
(with `--render`), regenerate it with:

```bash
python -m pysidtracker.pathdiff build $HVSC .hvsc-survey/tunes.csv --work .hvsc-survey \
    --left 6502 --right format:defmon --jobs 32 --force
python -m pysidtracker.pathdiff rank --work .hvsc-survey --left 6502 --right format:defmon \
    --explain 10
python -m pysidtracker.pathdiff xfails --work .hvsc-survey --left 6502 --right format:defmon \
    --manifest tests/_support/pathdiff_xfails.json --top 15 --verify \
    --as-left 6502 --as-right format:defmon
```

Existing cases are kept; add `--prune` to drop those that now pass. `rank` writes
`fixes.csv` and `explain/<rank>.txt` (whydiff) under
`.hvsc-survey/pathdiff/6502-format.defmon/`.

## What's in the package

- [`DefmonSong`](../pydefmon/defmon.py) — the 22887-byte runtime RAM image at
  `$1800..$7166`, parsed from a `.prg` file or `.sid` replay. Region accessors
  for arrangers (including the SID#2 stereo pages `arranger_v4`/`v5`/`v6`),
  sidTAB rows + DL bytes, pattern bodies, and the runtime-state page fields
  (`cia2_timer`, `sub_frame_count`, `stereo_enable`, `sid2_base`); structured
  edit API (`set_step`, `set_jump`, `set_dl`, `set_jp`, `set_pattern_events`).
  Round-trips through the `$D6C9` LOAD codec. A `.prg` and a `.sid` of the same
  tune are the same music in two forms — `form` reports `"editor"` vs
  `"packed"` and `diff(other)` compares them region-wise (rather than a raw
  `snapshot ==`, which is false across the two forms).
- [`PatternEvent`](../pydefmon/defmon.py) — one 4-byte pattern step
  (`flag, slot_a, slot_b, note`) with flag-bit constants and factories
  (`note_on`, `delay`, `alt_end`).
- [`SidtabRow`](../pydefmon/defmon.py) — one 15-byte sidTAB row decoded to named
  column attributes (`WGh / WGl / AD / SR / TR / AF / PW` low half; `PS / RE /
  FV / CP / ACID` high half) plus its companion `jp` and `dl` bytes.
- [`SidcallFrame`](../pydefmon/defmon.py) — one frame of a cascade walk via
  `DefmonSong.sidcall_frames(start_row)`.
- [`DefmonPlayer`](../pydefmon/defmon_player.py) — a `pysidtracker.SpecPlayer`
  that runs a `.sid` tune's replay as the playspec below (else its own relocatable
  replay on a jennings 6502, with the NMOS illegal opcodes defMON uses) and
  samples the 25 SID registers per frame.
  `render_grid(nframes)` and `play_frame()` come from the shared base; byte-exact
  against the `sidtrace` oracle.
- [`pydefmon/specs/`](../pydefmon/specs) — the replay as a `pysidtracker`
  playspec: `defmon.play` composes the per-block fragments (`defmon_band`,
  `_walker`, `_reader`, `_cascade`, `_sidrow`, `_pitch`, `_pwsweep`, `_acid`;
  `_nmi` is the editor's NMI counter, checked against the editor's handler
  but not part of `defmon.play`). `pydefmon._spec_layout.defmon_binding`
  binds it to a tune's relocated replay by signature. The routine is entered at the
  `JMP` to the band beside the player's start, at the band (called from the tune's
  own code), or at the player's start plus six, which runs the band and cutoff slide
  and the cascade but not the walker and readers (multi-speed tunes alternate the
  two). A call that overruns its period makes the replay miss the next interrupt; the
  spec costs no cycles, so `DefmonPlayer` plays that call and runs one call ahead of the
  replay's own 6502 from there. `DefmonPlayer` runs it
  (`spec_backed`; a stereo tune with a second copy of the player gets a second instance bound to its chip), and `tests/test_playspec_whole.py` holds it to the replay's
  6502 on every play call of each oracle song: registers and all memory outside
  the stack page. `tests/test_playspec_coverage.py` also holds it to the code by what the
  code does: for each of 15 replay builds a few random states
  (`tests/_support/routine_coverage.py`) run every instruction the play entries reach and
  both ways of every branch bar four dead ways (jennings' `CoverageMPU` measures this), the
  spec and the CPU leave the same 64 KiB and SID registers on each, and twelve one-line
  changes to the spec are told apart. `scripts/coverage_seeds.py` finds the seeds; the
  relocated `Glow_Worm`, `Rent-A-Cop` and `Timewarp_Spheres_2SID` follow stale `$1000`
  operands the spec does not model and are covered by the whole-song tests.
- `register_writes_from_player` / `read_reglog` / `write_reglog` / `RegWrite` —
  the shared `py*` register-log surface.
- `render_player_wav` / `render_player_samples` — the shared `py*` WAV/sample
  render; hand it a `DefmonPlayer`.
