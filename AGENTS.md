# AGENTS.md — pydefmon

Notes for coding agents.

## Layout

* `pydefmon/defmon.py` — `DefmonSong`, `PatternEvent`, `SidtabRow`,
  `SidcallFrame`. Tune reader / writer + format types.
* `pydefmon/defmon_player.py` — `DefmonPlayer` (a
  `pysidtracker.MemPlayer` running the tune's own replay on py65),
  `render_wav`, CLI entry point.
* `pydefmon/reglog.py` — the shared `py*` register-log surface.
* `pydefmon/_load_format.py` — `$D6C9` LOAD-time codec
  (round-trips byte-faithfully against the real defMON loader).
* `docs/format.md` — canonical reference for the file format and
  runtime RAM layout.
* `tools/fetch_fixtures.py` — downloads the upstream csdb.dk defMON
  release, extracts the `.d64`, and writes per-PRG files under
  `build/fixtures/`. Tunes are not redistributed; tests `skipTest`
  when fixtures are absent.

## Tests

* `tests/test_defmon.py` — `DefmonSong` / `PatternEvent` /
  `SidtabRow` data-class + edit API.
* `tests/test_sid_replay.py`, `tests/test_hvsc_sid_corpus.py` — the
  `.sid` replay decoder (synthetic + real HVSC corpus).
* `tests/test_player.py` — `DefmonPlayer` render / play_frame / WAV,
  driven by a runnable synthetic replay (offline) + a real replay.
* `tests/test_reglog.py` — the register-log surface.
* `tests/test_oracle_hvsc.py` — **byte-exact** oracle: marked
  `oracle`, excluded from the default suite; runs the player against
  the `anarkiwi/sidtrace` `sidplayfp` oracle over real HVSC tunes.
* `tests/test_lint.py` — `black --check` over the package + tests.

Run unit tests (excludes the `oracle` marker by default):

```
python -m tools.fetch_fixtures          # one-time, populates build/fixtures/
pytest -n auto
```

Run the byte-exact oracle (needs `docker` + the `anarkiwi/sidtrace`
image, or a warm `.oracle-cache/`):

```
pytest -m oracle -n auto
```

## Library gotcha worth remembering

`DefmonSong.clear_song_table()` wipes `$1B00..$1EFF` — **including**
the sidTAB DL byte region at `$1E00..$1EFF`. Always call it before
any `set_dl` writes; otherwise the cascade refetches every frame
instead of holding rows. Use the targeted `clear_arranger()` /
`clear_dl()` when you only need one or the other.

## License

GPL-3.0-or-later (see `LICENSE`).
