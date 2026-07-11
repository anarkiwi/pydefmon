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

`DefmonPlayer` runs the replay embedded in a `.sid` (a `.prg` editor workfile
carries no player). Construct it from bytes, a path, or a `SidImage`.

```python
from pydefmon import DefmonPlayer

player = DefmonPlayer("tune.sid")
for _ in range(600):
    writes = player.play_frame()           # (reg, value) list; reg is a 0..24 offset
    for reg, value in writes:
        ...

# Or a whole grid at once (nframes x 25 registers, forward-filled).
grid = DefmonPlayer("tune.sid").render_grid(250)
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

## What's in the package

- [`DefmonSong`](../pydefmon/defmon.py) — the 22887-byte runtime RAM image at
  `$1800..$7166`, parsed from a `.prg` file or `.sid` replay. Region accessors
  for arrangers, sidTAB rows + DL bytes, pattern bodies; structured edit API
  (`set_step`, `set_jump`, `set_dl`, `set_jp`, `set_pattern_events`).
  Round-trips through the `$D6C9` LOAD codec.
- [`PatternEvent`](../pydefmon/defmon.py) — one 4-byte pattern step
  (`flag, slot_a, slot_b, note`) with flag-bit constants and factories
  (`note_on`, `delay`, `alt_end`).
- [`SidtabRow`](../pydefmon/defmon.py) — one 15-byte sidTAB row decoded to named
  column attributes (`WGh / WGl / AD / SR / TR / AF / PW` low half; `PS / RE /
  FV / CP / ACID` high half) plus its companion `jp` and `dl` bytes.
- [`SidcallFrame`](../pydefmon/defmon.py) — one frame of a cascade walk via
  `DefmonSong.sidcall_frames(start_row)`.
- [`DefmonPlayer`](../pydefmon/defmon_player.py) — a `pysidtracker.EmuPlayer`
  that runs a `.sid` tune's own relocatable replay on a py65 6502 (with the NMOS
  illegal opcodes defMON uses) and samples the 25 SID registers per frame.
  `render_grid(nframes)` and `play_frame()` come from the shared base; byte-exact
  against the `sidtrace` oracle.
- `register_writes_from_player` / `read_reglog` / `write_reglog` / `RegWrite` —
  the shared `py*` register-log surface.
- `render_player_wav` / `render_player_samples` — the shared `py*` WAV/sample
  render; hand it a `DefmonPlayer`.
