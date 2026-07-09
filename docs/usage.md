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

```python
from pydefmon import DefmonPlayer

player = DefmonPlayer(song)
for _ in range(600):
    writes = player.play_frame()           # list of (reg, value) for one main-tick NMI
    for reg, value in writes:
        # reg is an absolute SID register address $D400..$D418
        ...
```

`DefmonPlayer.import_runtime_state()` bridges from a live VICE / hardware RAM
capture for byte-faithful continuation of a running tune (see
[format.md](format.md#bringing-pydefmon-up-against-a-real-c64)).

## Command line

Render to WAV (requires the `[wav]` extra):

```bash
pydefmon-player path/to/tune.prg /tmp/tune.wav
```

Dump per-frame SID writes as JSONL without rendering audio:

```bash
pydefmon-player --dump-writes /tmp/tune.jsonl path/to/tune.prg
```

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
- [`DefmonPlayer`](../pydefmon/defmon_player.py) — frame-accurate per-NMI player
  IRQ model. `play_frame()` returns the 24 SID register writes that defMON's
  `$1022` body would emit for one main player tick. `import_runtime_state()`
  bridges from a live VICE / hardware RAM capture.
- `iter_register_writes` / `read_reglog` / `write_reglog` / `RegWrite` — the
  shared `py*` register-log surface.
