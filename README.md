# pydefmon

Python implementation of the [defMON](https://csdb.dk/release/?id=199997)
C64 tracker's runtime data model, register-level player, and a CSV-to-prg
reconstructor.

The package is built from disassembly of the defMON binary's load-time
decoder (`$D6C9-$D74B`), NMI handler (`$0AED` body), sidcall cascade
(`$12DF`), pitch-slide oscillator (`$1405`), and ACID cutoff-slide
oscillator (`$10B5-$10D7`). Tests pin the Python model against expected
SID-register write streams produced by synthetic .prgs that exercise each
behaviour in isolation, plus a byte-faithful round-trip on the
[Glow Worm](https://csdb.dk/sid/?id=63421) demotune fixture.

## Layout

```
pydefmon/             core package
  defmon.py             on-disk format (DefmonSong, PatternEvent, SidtabRow)
  defmon_player.py      register-level player (DefmonPlayer, render_wav)
  dump_to_defmon.py     CSV write-log -> .prg reconstructor
  _load_format.py       $D6C9 LOAD-time codec
  data/                 binary assets (defmon_template.prg)
tests/                synthetic-prg foundational tests + round-trip pins
profile/              audit + recall scripts
fixtures/             glow_worm .csv / .prg + recon baseline
design/               design notes for the slides + layer-1 reverse-maps
tools/                docker-based round-trip harness
```

## Quick start

```bash
pip install -e .
python -m unittest discover -s tests
```

Render a .prg to wav (requires `pip install -e .[wav]` for pyresidfp):

```bash
python -m pydefmon.defmon_player fixtures/glow_worm.prg /tmp/glow_worm.wav
```

Reconstruct a .prg from a CSV write log:

```bash
python -m pydefmon.dump_to_defmon fixtures/glow_worm.csv /tmp/recon.prg
```

## Tests

```bash
python -m unittest discover -s tests
```

GitHub Actions runs the suite on push + PR (Python 3.10 / 3.11 / 3.12).
The round-trip test pins `build_song(glow_worm.csv).to_bytes()` byte-for-byte
against the committed recon baseline plus an exact-match recall figure on
the rendered CSV.

## License

GPL-3.0-or-later. See `LICENSE`.
