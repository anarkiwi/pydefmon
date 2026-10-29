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
row $01 WGh $41 AD $09 SR $f0 PW $08 hold 3 -> $02
row $02 WGh $40 stop
```

| Item | Meaning |
|------|---------|
| `jumps counted` / `uncounted` | Whether the player honours a jump row's repeat count (V20201008 and most HVSC replays do; older replays ignore the V3 byte). |
| `start $yy ...` | The arranger row each subtune starts at (`A` when the replay's init reaches the player). |
| `tempo $tttt xN` | CIA2 timer reload and NMIs per main tick (editor worktunes; packed replays zero it). |
| `stereo $dXXX` | SID#2 is enabled, at this base. |
| `sid N` | A chip's arranger: `$yy: $p1 $p2 $p3` plays three patterns, `$yy: jump $tt xC` continues at row `$tt`. |
| `pattern $pp` | Events at main-tick offsets from the pattern's first step: `a`/`b` arm sidcall layer 1/2 at a row, `n` sets the note, `end d` is the ALT step. |
| `row $rr` | Column writes in player order, then `hold F -> $nn` (fetch `$nn` after `F` frames) or `stop`. |

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
all jump rows. Only rows reached from a start row appear.

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

## Canonical form

`Tune.canonical()` maps every song to one representative of everything that
plays the same:

1. sidTAB rows are minimised as a Moore machine (output: columns and hold;
   transition: `next`) by partition refinement, so rows whose cascades produce
   the same writes forever are merged;
2. patterns are merged when their events are equal over those row classes;
3. patterns are numbered from `$01` in arranger order, and rows from `$01` in
   pattern-event order with each row's cascade numbered consecutively after it.
   This is the layout `to_song` can write with no JP rows except at loop-backs.

Equal canonical tunes play identically. The converse need not hold: a sound
that comes about through different structure, such as a merged duration nibble
on an ALT step that never ends a step, is not normalised.

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

## Not representable

`Tune.from_song` raises `DefmonError` when the player would read bytes that no
editor song defines. These are: a pattern played past its 32 decoded steps, an
arranger step none of whose patterns ends, a pattern number `>= $80`, and a JP
onto a JP row. On HVSC this flags the replays whose structure
`DefmonSong`'s replay reader cannot rebuild yet. Run
`python scripts/grammar_survey.py` to list them.
