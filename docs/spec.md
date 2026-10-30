# The play spec

`pydefmon/specs/defmon.play` is the replay's play routine as a `pysidtracker`
[playspec](https://github.com/anarkiwi/pysidtracker/blob/main/docs/playspec.md);
`DefmonPlayer` runs it. `pydefmon.spec` is its public module.

## API

| name | |
|------|-|
| `spec() -> Spec` | the parsed `defmon.play` (cached) |
| `spec_text(name) -> str` | source of the packaged spec `name` (`defmon`, `defmon_pitch`, ...) |
| `player_bases(mem) -> list[int]` | base address of each copy of the player (a stereo tune has one per chip) |
| `entry_base(mem, entry) -> int` | base of the copy `entry` enters; `ValueError` if none |
| `play_entries(mem, base) -> set[int]` | the modelled entries of the copy at `base` |
| `defmon_binding(mem, base, entry=None) -> dict` | the layout of `defmon.play` for the copy at `base`; `entry` selects the `reduced` flag |
| `build_facts(mem, base, entry=None) -> BuildFacts` | what the binding found (below) |
| `spec_entries(view) -> set[int]`, `spec_bind(view, entry) -> dict` | the `entries` and `bind` hooks of a `SpecPlayer` |

`BuildFacts(base, flags, tables, state, binding)` are read-only mappings of layout names to
values:

- `flags` (`BUILD_FLAGS`): `uncounted, plain_tr, or_ctrl, floored, shifts, fixed, reduced`
  as bools.
- `tables` (`BUILD_TABLES`): addresses of the pitch (`pitch_lo`, `pitch_hi`), slide-rate,
  arranger-page, pattern-pointer and sidTAB row tables.
- `state` (`BUILD_STATE`): addresses of the cells that hold the state after init (the
  arranger, reader and cascade counters and pointers, the cutoff slide, and voice 0's
  record; voice `n` is `n * VOICE_STRIDE` bytes on).
- `binding`: the whole layout.

## `SidFormat.spec`

`defmon_format().spec(model)` is a `pysidtracker.formats.SpecBinding` of the model's own
replay image and subtune, with `spec_entries` and `spec_bind`. So
`fmt.spec_player(model)` plays what `DefmonPlayer` plays; the SID model of the header is not
carried, so pass `sid_model=` (and `cycles_per_frame=` for a tune whose play call overruns
its period) as `DefmonPlayer` derives them.

## Standard notes

The `ro` cells over the note-pitch table declare which note each index means, so
`fmt.spec_player(model, notes=clock)` answers every read of the table with
`pysidtracker.notes.frequency(note, clock)`. The table is indexed by the note byte `k`,
which is standard note `k - 24` (`$30` is C2; indexes 0-23 are the notes C-2 to B-1).

| cell | address | declaration | reads |
|------|---------|-------------|-------|
| `pitch_lo`, `pitch_hi` | `L.pitch_*` | `notes(lo\|hi, -24)` | the voice's note `k` |
| `pitch_lo_step`, `pitch_hi_step` | `L.pitch_* - 24` | `notes(lo\|hi, -48)` | the portamento's upper index `y = mode + note` (8-bit sum) |
| `pitch_lo_prev`, `pitch_hi_prev` | `L.pitch_* - 25` | `notes(lo\|hi, -49)` | the portamento's lower index `y` |

An alias cell's index is its own, so its `K` is the base's offset plus the table's: a cell at
`L.pitch - c` indexed by `y` reads table entry `y - c`, note `y - c - 24`. Indexes are 8-bit, so
every note byte up to 255 is answered, past the native table's top and below its start. The
same declarations are in `defmon_pitch.play`. The relative-note (`plain_tr`) and absolute
builds differ only in how the note byte is formed, not in how it is read.
