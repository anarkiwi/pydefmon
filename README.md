# pydefmon

[![test](https://github.com/anarkiwi/pydefmon/actions/workflows/test.yml/badge.svg)](https://github.com/anarkiwi/pydefmon/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/pydefmon.svg)](https://pypi.org/project/pydefmon/)

Read, write and play [defMON](https://csdb.dk/release/?id=199997) C64
tracker tunes from Python. The player emits the same per-frame SID
register writes as the real defMON binary — verified byte-for-byte by
[a live-VICE integration test](tests/integration/test_player_vs_real.py)
that compares pydefmon's per-frame output against the actual defMON
binary running in `anarkiwi/headlessvice`.

## Install

```bash
pip install pydefmon          # core
pip install pydefmon[wav]     # adds pyresidfp + numpy for WAV render
```

## Quick start

pydefmon reads both containers a defMON tune ships in: the `.prg`
editor workfile defMON saves, and the PSID/RSID `.sid` *replay* files
HVSC distributes (the packed, relocatable player + compacted tune
data). `DefmonSong.from_bytes` / `DefmonSong.from_file` /
`DefmonSidParser().read` dispatch on the container automatically. See
[`docs/SPEC.md`](docs/SPEC.md#6-psidrsid-sid-replay-container) for the
`.sid` layout, including the older Goto80 compact-runtime encoding.

```python
from pydefmon import DefmonSong, DefmonPlayer, PatternEvent

# Read / edit / write tunes (.prg editor workfile or .sid replay)
song = DefmonSong.from_file("tune.prg")
print(song.pattern_events(0)[:4])
print(song.sidtab_row(0))
song.set_step(0, v1=1, v2=2, v3=3)        # arranger row 0 plays patterns 1/2/3
song.to_file("out.prg")

# Render per-frame SID writes
player = DefmonPlayer(song)
for _ in range(600):
    writes = player.play_frame()           # list of (reg, value) for one main-tick NMI
    for reg, value in writes:
        # reg is an absolute SID register address $D400..$D418
        ...
```

Render to WAV (requires the `[wav]` extra):

```bash
pydefmon-player path/to/tune.prg /tmp/tune.wav
```

Dump per-frame SID writes as JSONL without rendering audio:

```bash
pydefmon-player --dump-writes /tmp/tune.jsonl path/to/tune.prg
```

## What's in the package

- [`DefmonSong`](pydefmon/defmon.py) — the 22887-byte runtime RAM image
  at `$1800..$7166`, parsed from a `.prg` file. Region accessors for
  arrangers, sidTAB rows + DL bytes, pattern bodies; structured edit
  API (`set_step`, `set_jump`, `set_dl`, `set_jp`,
  `set_pattern_events`). Round-trips through the `$D6C9` LOAD codec.
- [`PatternEvent`](pydefmon/defmon.py) — one 4-byte pattern step
  (`flag, slot_a, slot_b, note`) with flag-bit constants and
  factories (`note_on`, `delay`, `alt_end`).
- [`SidtabRow`](pydefmon/defmon.py) — one 15-byte sidTAB row decoded
  to named column attributes (`WGh / WGl / AD / SR / TR / AF / PW`
  low half; `PS / RE / FV / CP / ACID` high half) plus its companion
  `jp` and `dl` bytes.
- [`SidcallFrame`](pydefmon/defmon.py) — one frame of a cascade walk
  via `DefmonSong.sidcall_frames(start_row)`.
- [`DefmonPlayer`](pydefmon/defmon_player.py) — frame-accurate
  per-NMI player IRQ model. `play_frame()` returns the 24 SID
  register writes that defMON's `$1022` body would emit for one
  main player tick. `import_runtime_state()` lets you bridge from
  a live VICE / hardware RAM capture for byte-faithful continuation
  of a running tune.

See [`docs/SPEC.md`](docs/SPEC.md) for the canonical reference of
the file format, the runtime RAM layout, and what every cell of
every region means.

## Tests

```bash
pip install -e .[dev]
python -m tools.fetch_fixtures        # one-time: populate build/fixtures/
python -m unittest discover -s tests -t .
```

The unit tests under `tests/` pin every subsystem of the player and
codec against synthetic `.prg` fixtures and against the .GLOW WORM
fixture (Tomek Grynfelder, in the upstream defMON release).
`tools/fetch_fixtures.py` downloads the upstream csdb.dk archive,
extracts the `.d64`, and writes per-PRG files under
`build/fixtures/` (override with the `PYDEFMON_FIXTURES_DIR` env
var). Tunes are not redistributed; tests that need a fixture
`skipTest` when it is missing.

The live-VICE integration test under `tests/integration/` boots the
real defMON binary inside `anarkiwi/headlessvice`, captures its SID
write stream, and asserts pydefmon's `DefmonPlayer` matches
byte-for-byte. Run with:

```bash
pip install -e .[dev,integration]
PYDEFMON_INTEGRATION=1 python -m unittest tests.integration.test_player_vs_real
```

CI runs all three (`test`, `lint`, `build`, `integration`) on push
and PR. Python 3.10 through 3.13.

## License

GPL-3.0-or-later. See [`LICENSE`](LICENSE).
