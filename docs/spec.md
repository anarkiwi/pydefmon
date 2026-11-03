# The play spec

`pydefmon/specs/defmon.play` is the replay's play routine as a `pysidtracker`
[playspec](https://github.com/anarkiwi/pysidtracker/blob/main/docs/playspec.md);
`DefmonPlayer` runs it. `pydefmon.spec` is its public module.

## Model

One call is one tick. The SID is written from what the last call left in each voice, then the
cutoff slide steps; the arranger moves on if a pattern ended; each voice's reader counts a step
out or reads the next; six sidTAB tables (two per voice) apply a row of column writes or wait out
its delay; the voices sound last (pitch, then the pulse-width sweep).

- Song data is a cell (the SID, the pitch tables) or a read-only list the binder lays out
  (the arranger's columns, the pattern and sidTAB row addresses, the delays, the slide rates).
- Every player variable is `state`, no address, started from what the replay's init left. A
  machine's state is one per voice (`voice[1].freq`) or per sidTAB table (`table[4].wait`).
  `SpecPlayer.state(name)` and `set_state(name, value)` read and set it.
- A pattern step and a sidTAB row are read as streams of the grammars in `streams.lark`.
- The variables of the replay's code that the spec holds no state for (the readers' copies of a
  step's flag, the row interpreter's pointer and scratch, the cutoff slide's opcodes) are
  not modelled.
- The routine's entries share the tune's state; the reduced entry (the player's start plus six)
  runs the same spec with the arranger and readers off (`L.reduced`).
- A replay with more than one copy of the player (stereo) is refused: the state is the tune's, not
  the copy's.

## API

| name | |
|------|-|
| `spec() -> Spec` | the parsed `defmon.play` (cached) |
| `spec_text(name) -> str` | source of the packaged spec `name` (`defmon`, `defmon_pitch`, ...) |
| `player_bases(mem) -> list[int]` | base address of each copy of the player |
| `entry_base(mem, entry) -> int` | base of the copy `entry` enters; `ValueError` if none |
| `play_entries(mem, base) -> set[int]` | the modelled entries of the copy at `base` |
| `player_places(mem, base, entry=None) -> dict` | where the copy keeps its tables, variables and build flags |
| `defmon_binding(mem, base, entry=None, places=None) -> dict` | the layout of `defmon.play` for the copy at `base` |
| `state_addresses(places) -> dict` | the byte addresses of each variable, by the name `SpecPlayer.state` takes |
| `variable_addresses(places) -> set[int]` | every byte the player writes as a variable |
| `build_facts(mem, base, entry=None) -> BuildFacts` | what the binder found (below) |
| `spec_entries(view) -> set[int]`, `spec_bind(view, entry) -> dict` | the `entries` and `bind` hooks of a `SpecPlayer` |

`defmon_binding` reports only facts read from the image: the SID and pitch-table addresses, the build's
flags (`uncounted`, `plain_tr`, `or_ctrl`, `shifts`, `fixed`, `reduced`, `cp_extra`), the song
data as lists, the constants of each voice (`detune`, `vbit`, `vmask`), the cutoff floor, and in
`init_<state>` the variables as init leaves them. It raises `ValueError` when a table's index can
reach a player variable (the lists are read once, so a variable there would be stale), and for a
stereo replay.

`BuildFacts(base, flags, tables, state, binding)` are read-only mappings of names to values:

- `flags` (`BUILD_FLAGS`): `uncounted, plain_tr, or_ctrl, floored, shifts, fixed, reduced` as
  bools.
- `tables` (`BUILD_TABLES`): addresses of the pitch (`pitch_lo`, `pitch_hi`), slide-rate,
  arranger-page, pattern-pointer and sidTAB row tables.
- `state` (`BUILD_STATE`): addresses of the cells that hold the state after init (the arranger,
  reader and sidTAB table counters and pointers, the cutoff slide, and voice 0's record; voice `n`
  is `n * VOICE_STRIDE` bytes on). `Tune.from_replay` reads the song from them.
- `binding`: `player_places`, every address.

## `SidFormat.spec`

`defmon_format().spec(model)` is a `pysidtracker.formats.SpecBinding` of the model's own
replay image and subtune, with `spec_entries`, `spec_bind` and the original `.sid` container
(`sid`, absent for a bare or export PRG). So `fmt.spec_player(model)` plays what `DefmonPlayer`
plays, taking the clock and SID model from the header, and `notes=True` answers with the
tune's own clock. A stereo (2SID) replay raises `SidFormatError`.

## Standard notes

The `ro` cells over the note-pitch table declare which note each index means, so
`fmt.spec_player(model, notes=clock)` (`True`: the tune's clock) answers every read of the table with
`pysidtracker.notes.frequency(note, clock)`. The table is indexed by the note byte `k`,
which is standard note `k - 24` (`$30` is C2; indexes 0-23 are the notes C-2 to B-1).

| cell | address | declaration | reads |
|------|---------|-------------|-------|
| `pitch` | `L.pitch_*` | `notes(word, -24)` | the voice's note `k` |
| `above` | `L.pitch_* - 24` | `notes(word, -48)` | the semitone step's upper index `y = mode + note` (8-bit sum) |
| `below` | `L.pitch_* - 25` | `notes(word, -49)` | the semitone step's lower index `y` |

An alias cell's index is its own, so its `K` is the base's offset plus the table's: a cell at
`L.pitch - c` indexed by `y` reads table entry `y - c`, note `y - c - 24`. Indexes are 8-bit, so
every note byte up to 255 is answered, past the native table's top and below its start. The
same declarations are in `defmon_pitch.play`. The relative-note (`plain_tr`) and absolute
builds differ only in how the note byte is formed, not in how it is read.
