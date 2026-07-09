# pydefmon

[![test](https://github.com/anarkiwi/pydefmon/actions/workflows/test.yml/badge.svg)](https://github.com/anarkiwi/pydefmon/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/pydefmon.svg)](https://pypi.org/project/pydefmon/)

Pure-Python reader, writer, and player for
[defMON](https://csdb.dk/release/?id=199997) C64 tracker tunes. The player emits
the same per-frame SID register writes as the real defMON binary, verified
byte-for-byte against it running in `anarkiwi/headlessvice`.

Consumes `.sid` files (PSID/RSID containers) and bare `.prg` images through the
shared [`pysidtracker`](https://github.com/anarkiwi/pysidtracker) base: it reads
both the `.prg` editor workfile defMON saves and the packed, relocatable PSID/RSID
`.sid` *replay* files HVSC distributes, reconstructing the same runtime image
either way — the relocatable player is located by signature and container headers
are not trusted.

## Install

```bash
pip install pydefmon          # core
pip install pydefmon[wav]     # adds pyresidfp + numpy for WAV render
```

## Usage

```python
from pydefmon import DefmonSong, DefmonPlayer

song = DefmonSong.from_file("tune.sid")    # .sid replay or .prg workfile; auto-dispatch
print(song.pattern_events(0)[:4])
print(song.sidtab_row(0))

# Render per-frame SID writes.
player = DefmonPlayer(song)
for _ in range(600):
    for reg, value in player.play_frame():  # reg is an absolute $D400..$D418 address
        ...
```

See [docs/usage.md](docs/usage.md) for editing, the register-write API, and the
`pydefmon-player` command line, and [docs/format.md](docs/format.md) for the
canonical file-format and runtime-RAM reference.

## Development

```bash
pip install -e ".[dev]"
python -m tools.fetch_fixtures        # one-time: populate build/fixtures/
python -m unittest discover -s tests -t .
```

## License

GPL-3.0-or-later. See [`LICENSE`](LICENSE).
