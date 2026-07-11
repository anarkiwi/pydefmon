# pydefmon

[![test](https://github.com/anarkiwi/pydefmon/actions/workflows/test.yml/badge.svg)](https://github.com/anarkiwi/pydefmon/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/pydefmon.svg)](https://pypi.org/project/pydefmon/)

Pure-Python reader, writer, and player for
[defMON](https://csdb.dk/release/?id=199997) C64 tracker tunes. `DefmonPlayer`
runs a tune's own relocatable replay on a py65 6502 and emits the per-frame SID
register writes, verified byte-exact against the
[`sidtrace`](https://github.com/anarkiwi/sidtrace) `sidplayfp` oracle over real
HVSC tunes.

Reads `.sid` files (PSID/RSID containers) and bare `.prg` editor workfiles
through the shared [`pysidtracker`](https://github.com/anarkiwi/pysidtracker)
base, reconstructing the same runtime image either way — the relocatable player
is located by signature and container headers are not trusted. The player runs
the replay embedded in a `.sid`.

## Install

```bash
pip install pydefmon
```

## Usage

```python
from pydefmon import DefmonSong, DefmonPlayer

# Read / edit a tune (either container auto-dispatches on magic).
song = DefmonSong.from_file("tune.sid")
print(song.pattern_events(0)[:4])
print(song.sidtab_row(0))

# Play a .sid replay: per-frame SID register writes / grid.
player = DefmonPlayer("tune.sid")           # bytes, path, or SidImage
for reg, value in player.play_frame():      # reg is a 0..24 SID register offset
    ...
grid = DefmonPlayer("tune.sid").render_grid(250)   # 250 x 25-register frames
```

See [docs/usage.md](docs/usage.md) for editing, the register-write API, and the
`pydefmon` command line (`info` / `reglog` / `wav`), and
[docs/format.md](docs/format.md) for the canonical file-format and runtime-RAM
reference.

## Development

```bash
pip install -e ".[dev]"
pytest -n auto
```

Tests that need real defMON tunes fetch + cache them on demand (the copyright
editor workfiles from the csdb release, the HVSC replays for the corpus) and
skip when offline; nothing needs pre-populating.

## License

GPL-3.0-or-later. See [`LICENSE`](LICENSE).
