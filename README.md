# pydefmon

[![test](https://github.com/anarkiwi/pydefmon/actions/workflows/test.yml/badge.svg)](https://github.com/anarkiwi/pydefmon/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/pydefmon.svg)](https://pypi.org/project/pydefmon/)

Pure-Python reader, writer, and player for
[defMON](https://defmon.vandervecken.com) C64 tracker tunes (latest
[V20201008](https://csdb.dk/release/?id=196474) on csdb). `DefmonPlayer`
runs a tune's own relocatable replay on a jennings 6502 and emits the per-frame SID
register writes, verified byte-exact over whole songs against the
[`sidtrace`](https://github.com/anarkiwi/sidtrace) `sidplayfp` oracle on real
HVSC tunes. Each play call runs the replay as a
[`pysidtracker` playspec](pydefmon/specs/defmon.play) (`SpecPlayer`), held to the
replay's own 6502 over whole songs; a replay the spec does not describe runs its 6502.

Reads `.sid` files (PSID/RSID containers), bare `.prg` editor workfiles, and
defMON's own pack-save export PRGs (SHIFT+P: the `$1000` raw export and the
`$0801` executable with its BASIC stub + host boot) through the shared
[`pysidtracker`](https://github.com/anarkiwi/pysidtracker) base, decoding all
of them to the same `$1800..$7166` structural model — including the stereo
(2SID) SID#2 arranger pages and the per-tune tempo fields on the
runtime-state page. Prehistoric 2008-build
worktunes (raw `$1700` saves with no RLE codec) are detected by load address
and rejected with a specific `DefmonParseError` (see
[docs/format.md](docs/format.md#prehistoric-2008-builds-raw-1700-worktunes)). A `.prg` workfile is the
composer's editor form, the export PRGs and `.sid` the packer's canonical form
— the same tune, not the same bytes — compare them with `DefmonSong.diff` (not
a raw `snapshot ==`). The relocatable player is located by signature; container
headers are not trusted; `DefmonPlayer` plays PSID/RSID replays and export PRGs, and rejects `.prg` workfiles (use `DefmonSong` to read those).

## Install

```bash
pip install pydefmon
```

## Usage

```python
from pysidtracker import song_frames
from pydefmon import DefmonSong, DefmonPlayer

# Read / edit a tune (either container auto-dispatches on magic).
song = DefmonSong.from_file("tune.sid")
print(song.pattern_events(0)[:4])
print(song.sidtab_row(0))

# Play a .sid replay: per-frame SID register writes / grid.
player = DefmonPlayer("tune.sid")           # bytes, path, or SidImage
for reg, value in player.play_frame():      # reg is a 0..24 SID register offset
    ...
grid = DefmonPlayer("tune.sid").render_grid(song_frames("tune.sid"))   # whole song x 25 registers
```

`Tune.from_song(song)` reduces a song to what its player reads, as text in a
[lark grammar](docs/grammar.md) with a canonical form for semantic comparison.

See [docs/usage.md](docs/usage.md) for editing, the register-write API, and the
shared `pysidtracker` command line (`info` / `reglog` / `wav`, which pydefmon
plugs into), and [docs/format.md](docs/format.md) for the canonical file-format
and runtime-RAM reference.
The [HVSC oracle survey](docs/usage.md#hvsc-oracle-survey) runs the player against every HVSC defMON tune.

## Development

```bash
pip install -e ".[dev]"
pytest -n auto
```

Tests that need real defMON tunes fetch + cache them on demand (the copyright
editor workfiles from the csdb release, the HVSC replays for the corpus) into
pysidtracker's `.tunecache` (`$PYSID_TUNECACHE`) and skip when offline; nothing
needs pre-populating.

## License

GPL-3.0-or-later. See [`LICENSE`](LICENSE).
