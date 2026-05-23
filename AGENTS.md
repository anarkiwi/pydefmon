# AGENTS.md — pydefmon

Notes for coding agents.

## Layout

* `pydefmon/defmon.py` — `DefmonSong`, `PatternEvent`, `SidtabRow`,
  `SidcallFrame`. Tune reader / writer + format types.
* `pydefmon/defmon_player.py` — `DefmonPlayer`, `Voice`, `render_wav`,
  CLI entry point. Frame-accurate player IRQ model.
* `pydefmon/_load_format.py` — `$D6C9` LOAD-time codec
  (round-trips byte-faithfully against the real defMON loader).
* `docs/SPEC.md` — canonical reference for the file format, the
  runtime RAM layout, and what every region means.
* `tools/fetch_fixtures.py` — downloads the upstream csdb.dk defMON
  release, extracts the `.d64`, and writes per-PRG files under
  `build/fixtures/` (override with `PYDEFMON_FIXTURES_DIR`).
  Tunes are not redistributed; tests `skipTest` when fixtures are
  absent.

## Tests

* `tests/test_defmon.py` — `DefmonSong` / `PatternEvent` /
  `SidtabRow` data-class API.
* `tests/test_defmon_player_portamento.py` — pitch slide ($1405).
* `tests/test_defmon_slides_model.py` — pitch slide + PS oscillator.
* `tests/test_defmon_layer1_model.py` — layer-1 sidcall cascade
  (ACID abs/SBC/ADC, GATE_B re-arm, slot_b=0 stop, RE bitmask).
* `tests/test_cutoff_slide_model.py` — cutoff slide model
  ($10B5-$10D7) in isolation.
* `tests/test_lint.py` — `black --check` over the package + tests.
* `tests/integration/test_player_vs_real.py` — live-VICE
  per-frame SID write comparison against the real defMON binary
  in `anarkiwi/headlessvice`. Opt-in via `PYDEFMON_INTEGRATION=1`.

Run unit tests:

```
python -m tools.fetch_fixtures          # one-time, populates build/fixtures/
python -m unittest discover -s tests -t .
```

Run the live-VICE integration test (needs `docker` + the
`anarkiwi/headlessvice` image + `pydefmon[integration]`):

```
pip install -e .[dev,integration]
python -m tools.fetch_fixtures --keep-d64
PYDEFMON_INTEGRATION=1 python -m unittest tests.integration.test_player_vs_real
```

## Library gotcha worth remembering

`DefmonSong.clear_song_table()` wipes `$1B00..$1EFF` — **including**
the sidTAB DL byte region at `$1E00..$1EFF`. Always call it before
any `set_dl` writes; otherwise the cascade refetches every frame
instead of holding rows. Use the targeted `clear_arranger()` /
`clear_dl()` when you only need one or the other.

## License

GPL-3.0-or-later (see `LICENSE`).
