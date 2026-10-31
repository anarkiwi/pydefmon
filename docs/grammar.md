# defMON song grammar

`pydefmon` reads and writes a defMON song as text in
[`pydefmon/defmon.lark`](../pydefmon/defmon.lark), a [lark](https://github.com/lark-parser/lark)
grammar for **what the replay player reads**. The editor writes far more than
that, including dead pattern steps, sidTAB columns no cascade fetches, JP rows,
and unreachable arranger rows. The grammar keeps only what reaches the SID, so
two songs can be compared, minimised and edited as structure instead of RAM
images or 6502 code.

```python
from pydefmon import DefmonSong, Tune

tune = Tune.from_song(DefmonSong.from_file("tune.prg"))   # .prg, .sid or export PRG
text = str(tune)                                          # the grammar's text
assert Tune.parse(text) == tune
canon = tune.canonical()                                  # merged + renumbered
canon.to_song().to_file("out.prg")                        # an editor worktune again

replay = Tune.from_player("tune.sid")                     # the replay's memory after init
```

## The language

```
jumps counted
start $00
tempo $4cc7 x1
sid 1
  $00: $01 $02 $03
  $01: $01 $04 $03
  $02: jump $00 x0
pattern $01
  0: n $31 a $01
  6: a $03 b $04
  62: end 0
row $01 WGh $40 WGl $01 AD $09 SR $f0 TR -- AF -- PW $08 PS -- RE -- FV -- CP -- ACID -- hold 3 -> $02
row $02 WGl $00 stop
```

| Item | Meaning |
|------|---------|
| `jumps counted` / `uncounted` | Whether the player honours a jump row's repeat count (V20201008 and most HVSC replays do; older replays ignore the V3 byte). |
| `start $yy ...` | The arranger row each subtune starts at (`A` when the replay's init reaches the player). |
| `tempo $tttt xN` | CIA2 timer reload and NMIs per main tick (editor worktunes; packed replays zero it). |
| `stereo $dXXX` | SID#2 is enabled, at this base. |
| `sid N` | A chip's arranger: `$yy: $p1 $p2 $p3` plays three patterns, `$yy: jump $tt xC` continues at row `$tt`. |
| `pattern $pp` | Events at main-tick offsets from the pattern's first step: `a`/`b` arm sidcall layer 1/2 at a row, `n` sets the note, `end d` is the ALT step. |
| `row $rr` | Column writes in player order, then `hold F -> $nn` (fetch `$nn` after `F` frames) or `stop`. A column is `--` (or omitted) when the row does not write it, and the voice keeps that column's last value; `$00` is a write of zero. Rows are printed with all twelve columns, as in the editor. |

## What the player reads

These are the read rules of the replay player (`$1000` body of every packed
replay; the editor's own live player is the same code with fixed 4-byte
pattern steps).

**Arranger.** On a step change the walker reads row `Y`. If it got to `Y` by
stepping from the previous row, V1 bit 7 marks a jump: V2 is the target and,
with counted jumps, V3 the count (`0` jumps forever; otherwise the first visit
loads the count, later visits decrement it, and at zero the walker falls
through). The row a jump lands on, and the row a counted jump falls through
to, are loaded as patterns without that check. One loop counter is shared by
all jump rows and belongs to the row that last loaded it, so a different counted
jump row restarts it. Only rows reached from a start row appear.

**Patterns.** A step whose duration nibble is `d` lasts `d + 2` main ticks:
the step is fetched when its voice's counter underflows and applied on the
next tick. Event times are therefore sums of `d + 2`, and a step that gates
nothing is only time, so it never appears. The ALT step (`end d`) applies its
gates and then ends the arranger step for **every** voice. The next tick runs
the arranger and loads all three counters with `d`. A step therefore lasts
until its voices' earliest ALT, and pattern events after that tick are never
applied. Each pattern keeps only events up to the latest tick any of its
arranger steps reaches. A pattern none of whose steps reach its ALT has no
`end`.

**sidTAB rows.** A pattern slot arms the cascade at a row, with counter `0`.
At counter `0` the cascade fetches the row: a row whose `$1900` marker is zero
is a JP and redirects (one level only) to the row in its `$1800` byte. It then
applies the row's columns, loads the counter with the row's DL and continues
after the effective row. DL `$80..$FF` stops the cascade; otherwise the row
holds `DL + 1` frames. JP rows therefore never appear: a slot that arms a JP
names its target, and a row followed by a JP has that target as `next`. Columns
are those whose bitmap bits the player tests (low bits 7..1, high bits 7..3), in
the order it reads them. Unused bitmap bits and trailing bytes are dropped.

A row the walker lands on (a jump's target, or the row after a counted jump's last pass) is
played as patterns whatever its V1; `land $yy: $p1 $p2 $p3` lines under a `sid` list those whose
V1 has bit 7 (`Tune.landed`). A row can be both a jump row, stepped onto, and a landing.

Arranger rows, pattern bodies and sidTAB rows are read through the byte grammar
[`pydefmon/streams.lark`](../pydefmon/streams.lark): rule `editor` for the
editor's 4-byte steps, `packed` for the editor's decode of a replay's stream (one payload byte per
set gate, 32 steps at most); `stream` and `fixed` (4-byte steps) for the streams the replay's
step reader reads, which run to the first ALT step with no step cap, `arranger` for a chip's reached arranger rows (three bytes each; V1 bit 7
marks a jump row) and `row` for a sidTAB row, whose bitmap bits select the column
values after them (`pysidtracker.BitField`). `pysidtracker grammar SONG` prints
the streams the player reads.

## Canonical form

`Tune.canonical()` maps every song to one representative of everything that
plays the same:

1. sidTAB rows are minimised as a Moore machine (output: columns and hold;
   transition: `next`) with `pysidtracker.automaton.minimise`, so rows whose
   cascades produce the same writes forever are merged;
2. patterns are merged when their events are equal over those row classes;
3. patterns are numbered from `$01` in arranger order, and rows from `$01` in
   pattern-event order with each row's cascade numbered consecutively after it.
   This is the layout `to_song` can write with no JP rows except at loop-backs.

Equal canonical tunes play identically. The converse need not hold: a sound
that comes about through different structure, such as a merged duration nibble
on an ALT step that never ends a step, is not normalised.

`pysidtracker equiv A B` compares two tunes' canonical texts and prints the
diff. `pysidtracker snapshot build CORPUS --format defmon` digests each tune's
decoded RAM image (probe `snapshot`) and canonical text (probe `canonical`),
and `pysidtracker snapshot diff` compares two such snapshots.

`DefmonSong.diff` compares RAM regions. Comparing canonical tunes is the
semantic comparison. A `.prg` worktune and defMON's own packing of it give
equal canonical tunes (apart from the zeroed tempo) unless the packer changed
the music.

## Writing

`Tune.to_song()` writes an editor-form `DefmonSong` with the post-LOAD gates set,
every non-JP sidTAB slot marked active (as the editor saves them), patterns as
fixed 4-byte steps, and gaps longer than one step split into the fewest
empty steps. Rows keep their numbers when every successor other than the next
row can take a JP row in the slot after it. Otherwise rows are laid out
again from slot `$01`, sources first, with one JP row closing each chain that
runs into a row already placed.

## From the replay's memory

`Tune.from_replay(mem, base=None, entry=None)` reads the 64 KiB a replay's init leaves, as
`specs/defmon.play` does: `build_facts` / `defmon_binding` (`pydefmon.spec`) locate the tables of
the copy of the player at `base`, and nothing is read from the editor's snapshot.
`Tune.from_player(source, subtune=None)` runs `DefmonPlayer`'s init first.

- An arranger row is three bytes; a pattern number is any byte, its pointer read from `pat_lo` /
  `pat_hi` at that index, past the 128 entries the editor stores.
- A pattern is the stream at its pointer (`stream`, or `fixed` for the build with 4-byte steps) up
  to its first ALT step wherever that falls, the address wrapping past `$FFFF`; it may continue
  into the memory after it. No ALT step in 64 KiB raises `DefmonError`.
- A JP sidTAB row (`rows_hi` 0) names `rows_lo` one level deep. A target that is itself a JP row is
  the row read from page zero at `rows_lo`, whatever init left there; one that overlaps the cells
  the row interpreter writes (`rowptr`, `scratch`) raises `DefmonError`.
- The walk starts at the arranger cursor init leaves. `Tune.init` (`Init`) holds the walker's
  `trigger`, `cursor`, `owner` and `count` (0 without loop counts), the cells shared by the voices
  (`cells`: the cutoff slide, `res`, `fv`) and each voice's (`voices`), by the names of
  `pydefmon.spec.BUILD_STATE`.
- The reduced entry runs no walker or readers and raises; a stereo replay is read one copy of the
  player at a time (`base`).

The model of a replay holds what `Tune.from_song` does for an editor song, plus `landed` and
`init`; `canonical()`, text and `parse` work on both. `to_song()` raises `DefmonError` for a tune
the editor cannot store: a landing, a pattern number `>= $80`.

## Not representable

`Tune.from_song` raises `DefmonError` when the player would read bytes that no
editor song defines. These are: a pattern played past its 32 decoded steps, an
arranger step none of whose patterns ends, a pattern number `>= $80`, a row landed on
whose V1 has bit 7, and a JP onto a JP row. On HVSC this flags the replays whose structure
`DefmonSong`'s replay reader cannot rebuild; `Tune.from_replay` reads them. Run
`python scripts/grammar_survey.py` to list them.
