# defMON tune format — pydefmon reference

Canonical reference for the defMON `.prg` file format and runtime
RAM layout as consumed by `pydefmon`. The
[live-VICE integration test](../tests/integration/test_player_vs_real.py)
pins pydefmon's `DefmonPlayer` against the real defMON binary
running in `anarkiwi/headlessvice` and asserts byte-for-byte
per-frame SID-register-write equivalence, so the player model is
authoritative for behaviour and this document is authoritative for
the byte layout.

The .GLOW WORM tune (by Tomek Grynfelder, in the upstream defMON
release on csdb.dk) is the canonical real-tune fixture. Tunes are
not redistributed with `pydefmon`; run
`python -m tools.fetch_fixtures` to populate `build/fixtures/`.

---

## 1. PRG container

defMON tunes are C64 KERNAL-loadable PRG files:

| Field                  | Value                                                       |
|------------------------|-------------------------------------------------------------|
| Load address (header)  | `$1800` (`$00 $18`)                                         |
| Body                   | `$D6C9`-encoded payload of `RAM[$1800..$7166]`              |
| End address            | per-tune, around 4–17 KB                                    |

defMON's LOAD secondary address is `$00`, which forces the load
destination to the SETLFS `(X,Y)` operand (`$1800`) regardless of
the file's embedded load address — so the file always lands at
`$1800` even if its 2-byte header says otherwise.

`pydefmon` reads the file via `DefmonSong.from_file()` and writes
via `DefmonSong.to_file()`. Both go through
[`pydefmon._load_format`](../pydefmon/_load_format.py)'s `$D6C9`
codec, which round-trips byte-for-byte against the real defMON
loader (a tune saved by `to_file()` is loadable by the original
binary).

---

## 2. Runtime RAM image: `$1800..$7166`

After LOAD + defMON's `$CF42` / `$D004` post-LOAD fixup passes, the
runtime RAM image is 22887 bytes laid out as:

| Address              | Length  | Region                                              | `DefmonSong` accessor |
|----------------------|---------|-----------------------------------------------------|-----------------------|
| `$1800..$18FF`       | 256 B   | Per-row JP-target pointer lo / runtime ptr lo       | `song_position_arrays` |
| `$1900..$19FF`       | 256 B   | Per-row JP marker (`$11`=active, `$00`=JP source)   | `sidtab_jp`            |
| `$1A00..$1A7F`       | 128 B   | Pattern pointer cache lo (zero on disk; runtime)    | `pattern_pointer_table` |
| `$1A80..$1AFF`       | 128 B   | Pattern pointer cache hi (zero on disk; runtime)    | (same)                  |
| `$1B00..$1BFF`       | 256 B   | V1 arranger (per-step pattern index)                | `arranger_v1`          |
| `$1C00..$1CFF`       | 256 B   | V2 arranger                                         | `arranger_v2`          |
| `$1D00..$1DFF`       | 256 B   | V3 arranger                                         | `arranger_v3`          |
| `$1E00..$1EFF`       | 256 B   | sidTAB DL byte (hold / STop)                        | `sidtab_dl`            |
| `$1F00..$5EFF`       | 16384 B | 128 pattern bodies × 128 B each                     | `pattern_bodies`, `pattern(n)` |
| `$5F00..$7166`       | 4711 B  | 256 sidTAB rows × 15 B each + sidcall metadata      | `sidtab_region`, `sidtab_row(y)` |

Bytes past `$7166` are editor state and not preserved.

### Song-position arrays (`$1800` + `$1900`)

For each song-step `Y` in `0..255`, the cascade decides whether the
row is **active linear** (cascade advances to row `Y+1` after DL
underflow) or a **JP source** (cascade jumps to a different row
instead) by reading `$1900,Y`:

* `$1900,Y == $11` — active linear. defMON's `$CF42` post-LOAD pass
  rewrites both bytes: `$1800,Y` becomes the runtime pointer-lo
  `(SIDTAB_BASE + Y*15) & $FF`, `$1900,Y` becomes the pointer-hi
  `((SIDTAB_BASE + Y*15) >> 8)`. The cascade dereferences this pair.
* `$1900,Y == $00` — JP source (or inactive if `$1800,Y == 0` too).
  `$1800,Y` holds the row index the cascade jumps to instead of
  advancing linearly. `$CF42` leaves both bytes alone.

`DefmonSong.set_jp(row, target=None | K)` / `jp_target(row)` are the
high-level edit API; reads return the on-disk marker form, not the
runtime-rewritten form.

### Pattern pointer cache (`$1A00..$1AFF`)

Zero on disk; defMON's `$D004` post-LOAD pass fills it at runtime
with the lo/hi of `$1F00 + X*$80` for `X` in `0..127`.
`DefmonSong.unpacked_snapshot()` returns a copy with this region
materialised, matching what the player sees at runtime.

### Voice arrangers (`$1B00`, `$1C00`, `$1D00`)

Per song-step `Y`, the byte at `$1B00,Y` / `$1C00,Y` / `$1D00,Y`
is the pattern number that voice V1/V2/V3 plays on this step.
Encoding:

* `$00` — silent voice on this step.
* `$01..$7F` — pattern number (high bit, if set, is an editor-only
  flag masked off by the player).
* `$FF` (V1 only) — jump command. `$1C00,Y` becomes the target song
  step; `$1D00,Y` is the repeat count (`0` = unconditional infinite
  loop).

Edit via `DefmonSong.set_step(y, v1=..., v2=..., v3=...)` for
pattern-playing rows and `DefmonSong.set_jump(y, target=..., count=...)`
for jumps.

### sidTAB DL byte (`$1E00..$1EFF`)

Per sidTAB row `Y`:

* `$00..$7F` — hold this row for `dl + 1` frames, then advance per
  the JP marker.
* `$80..$FF` — STop: cascade freezes; the slot does not advance
  until a `PatternEvent` with `GATE_A` / `GATE_B` re-arms it.

Set via `DefmonSong.set_dl(row, value)`.

### Pattern bodies (`$1F00..$5EFF`)

128 patterns × 32 events × 4 bytes = 16384 bytes. Each event:

| Byte | Field    |
|------|----------|
| 0    | flag — `ALT` / `GATE_A` / `GATE_B` / `GATE_N` / duration nibble |
| 1    | `slot_a` — sidTAB row to arm sidcall layer 1 (gated by `GATE_A`) |
| 2    | `slot_b` — sidTAB row to arm sidcall layer 2 (gated by `GATE_B`) |
| 3    | `note` — note byte `1..120`, or `0` for "no note"                |

Flag bits:

| Bit | Mask  | Const           | Effect                                                 |
|-----|-------|-----------------|--------------------------------------------------------|
| 7   | `$80` | `FLAG_ALT`      | Pattern-end / song-advance trigger                     |
| 6   | `$40` | `FLAG_GATE_A`   | Apply `slot_a` to sidcall layer 1 row index            |
| 5   | `$20` | `FLAG_GATE_B`   | Apply `slot_b` to sidcall layer 2 row index            |
| 4   | `$10` | `FLAG_GATE_N`   | Apply `note` as the voice's `current_note` + transpose buffer; zero slide state |
| 3-0 | `$0F` |                 | Duration (frames to hold this row before reading next) |

`PatternEvent.parse(raw)` decodes; factories `note_on()`,
`delay()`, `alt_end()`, `silent_pattern()` build canonical events;
read via `DefmonSong.pattern_events(idx)` and write via
`set_pattern_events(idx, events)`.

#### Note byte → SID frequency

The pitch oscillator indexes
`NOTE_PITCH_LO / NOTE_PITCH_HI` directly by `note` (not `note-1`).
`PatternEvent.sid_freq_word()` returns the same 16-bit value the
player will latch onto `$D400/$D401` (without the per-voice
detune; V0=+0, V1=+1, V2=+2 added by the player on `freq_lo`).

### sidTAB rows (`$5F00..` × 15 bytes)

Each row is a packed bitmap-of-columns record. The cascade fetches
one row per voice per cascade event and applies the row's columns
to that voice's working state on the frame of the fetch.

```
byte 0           low-half bitmap (which low columns are present)
bytes 1..N       low-half values (in screen-column order)
byte 1+N         high-half bitmap (which high columns are present)
bytes 2+N..M     high-half values (ACID is 2 bytes; others 1)
bytes M+1..14    zero pad
```

A column is absent (`None` on the parsed `SidtabRow`) when its bit
isn't set in the bitmap. Column meanings:

| Bitmap | Bit | Column | What it does                                                |
|--------|-----|--------|-------------------------------------------------------------|
| low    | 6   | `WGh`  | Sets `ctrl_main` (voice ctrl: waveform / gate / sync / ring; emitted to `$D404`) |
| low    | 7   | `WGl`  | Sets `ctrl_eor`: XOR mask applied to `ctrl_main` each frame before emit |
| low    | 5   | `AD`   | Sets the voice's attack/decay byte (emitted to `$D405`)     |
| low    | 4   | `SR`   | Sets the voice's sustain/release byte (emitted to `$D406`)  |
| low    | 3   | `TR`   | Sets `current_note`: bit 7 set = absolute; bit 7 clear = adds to transpose buffer (= last GATE_N note) |
| low    | 2   | `AF`   | Sets `slide_mode`: `$00` = no slide, `$01..$7F` = portamento, `$80..$FF` = active slide |
| low    | 1   | `PW`   | Sets `pulse_hi = byte`, `pulse_lo = byte & $F0` (12-bit `$YX` PW encoding; emitted to `$D402/$D403`) |
| high   | 7   | `PS`   | Sets `ps_depth`: pulse-width sweep depth + direction (bit 7 = direction) |
| high   | 6   | `RE`   | Sets `$D417` resonance + voice-routing (3-way dispatch on byte value) |
| high   | 5   | `FV`   | Sets `$D418` filter mode + volume (volume always emitted as `$0F`) |
| high   | 4   | `CP`   | Sets cutoff-slide saturation-step extra (added to `$D416` each frame) |
| high   | 3   | `ACID` | 16-bit cutoff-slide command (low byte = step; high byte = direction + control: bit 7 = slide-vs-absolute, bit 6 = SBC vs ADC) |

`SidtabRow.parse(index, raw, *, jp=None, dl=None)` decodes the row;
`pack({col: val})` / `to_bytes()` go back. Walk a cascade from row
`Y` with `DefmonSong.sidcall_frames(Y)` — returns a
`list[SidcallFrame]` terminating at the first STop, JP loopback, or
`max_frames`.

---

## 3. Player IRQ behaviour (`pydefmon.DefmonPlayer`)

The player runs at a tune-specific rate. defMON installs a CIA-2
Timer-A NMI with reload `(($715A | $715B << 8)) * $715C` cycles
(`$715A/$715B` = per-NMI cycle count, `$715C` = sub-frame ratio).
The CIA fires `sub_frame_count` NMIs per main player tick.

* **All NMIs** run the SID-write band (24 writes), the global
  $D417/$D418/$D416 writes, the ACID cutoff slide, the sidTAB
  cascade arms for both layers × 3 voices, the pitch-slide
  oscillator, and the pulse-width sweep.
* **Main-tick NMIs** (every `$715C`-th) additionally run the
  arranger walk + per-voice pattern advance, BEFORE the cascade.

### SID write band order

Each main tick emits exactly 24 SID register writes, in order:

```
V0 PW_lo, PW_hi, F_lo, F_hi, SR, AD, CTRL   (7)
V1 PW_lo, PW_hi, F_lo, F_hi, SR, AD, CTRL   (7)
V2 PW_lo, PW_hi, F_lo, F_hi, SR, AD, CTRL   (7)
$D417 (resonance + voice routing)            (1)
$D418 (volume + filter mode)                 (1)
$D416 (filter cutoff hi)                     (1)
```

`CTRL` is emitted as `ctrl_main XOR ctrl_eor` (so the sidTAB `WGl`
column flicks waveform/gate bits each frame).

### Sidcall cascade

Per voice × layer, the cascade tracks `(row_idx, step_counter)`:

* `counter & $80` set → frozen / STop; no advance.
* `counter == 0` → fetch row at `row_idx`, apply via the sidTAB
  bitmap walk, store `$1E00,row_idx` (DL byte) into the counter,
  advance `row_idx` to `(effective_row + 1) & $FF`.
* `counter in $01..$7F` → decrement, no fetch.

A pattern `GATE_A` / `GATE_B` re-arms by setting the layer's
`row_idx` and resetting `counter := 0`. A JP redirect at fetch time
(`$1900,row_idx == 0`) substitutes the target row from `$1800,row_idx`
and the cascade resumes linearly from `target + 1`.

### Pitch slide

Per voice, `slide_mode` selects the path:

* `0` — no slide. `freq = NOTE_PITCH[note] + pitch_base` (per-voice
  detune V0=0, V1=1, V2=2 on `freq_lo`; carry NOT propagated to
  `freq_hi`).
* `$01..$7F` — portamento toward `(note + slide_mode)`. Adds a
  one-frame adjacent-semitone step; not integrated across frames.
* `$80..$FF` — active slide. Index a per-mode rate LUT, integrate
  into a 16-bit accumulator, then add to `NOTE_PITCH[note]`.

`GATE_N` from a pattern step zeros the slide accumulator and the
slide mode, so a fresh note never inherits the prior row's slide.

### Pulse-width sweep (PS)

`ps_depth` (set via sidTAB `PS`) modulates `pulse_lo` each frame:

* `$00` — no-op.
* Bit 7 set (`$80..$FF`) — ADD path. `pulse_lo += (depth & $7F)`
  with carry into `pulse_hi`. On `pulse_hi == $0F` overflow, clamp
  `pulse_lo = $F8` and flip direction (XOR `depth ^= $80`).
* Bit 7 clear (`$01..$7F`) — SUB path. `pulse_lo -= depth - 1`
  (the `-1` comes from defMON's uncontrolled carry-in at the SBC).
  On `pulse_hi == 0` borrow, clamp `pulse_lo = $01` and flip
  direction.

Because the `PW` column only writes the high nibble of `pulse_lo`
(`byte & $F0`), tunes can't directly set `pulse_lo`'s low nibble —
the PS sweep is the only way to access those bits.

### ACID cutoff slide

A 16-bit accumulator (lo / hi) integrates a signed step per NMI:

* Lo byte: `acc_lo = ADC/SBC(acc_lo, step_lo, carry_in=0)`. The
  CLC-then-SBC idiom subtracts an extra 1, so SBC effectively does
  `acc_lo - step_lo - 1`.
* Hi byte: `acc_hi = ADC/SBC(acc_hi, step_hi, carry_from_lo)`. If
  the result has bit 7 set, reload `acc_hi = floor` (saturation
  clamp).
* Output: `A = acc_hi + cutoff_extra + carry_from_hi`. If `A & $80`
  or `A < $02`, output `floor` instead. Optional ASL doubles
  output. Written to `$D416`.

The `ACID` sidTAB column carries a 16-bit `(low, high)` command:

* `high & $80` clear → absolute reset: `acc_lo := low`,
  `acc_hi := high`, `step_lo/hi := 0`.
* `high & $80` set → slide configuration: `step_lo := low`. If
  `high & $40` set, opcode := SBC (subtract) and `step_hi := high & $3F`.
  Else opcode := ADC (add) and `step_hi := high & $7F`.

The `CP` sidTAB column sets `cutoff_extra` directly.

---

## 4. Bringing pydefmon up against a real C64

`DefmonPlayer.import_runtime_state(ram, base_addr)` lets you bridge
from a live VICE / hardware RAM capture into pydefmon, then resume
playback from the captured state. The integration test uses this
to compare pydefmon's per-frame writes against real defMON: it
halts the live binary at the player IRQ entry, snapshots RAM, seeds
pydefmon's player from the snapshot, and asserts byte equality on
each subsequent frame.

The needed snapshot range is `$1019..$7166` — covers per-voice
operand slots ($1019..$10AF), filter globals, cascade state,
current_note slots, sidcall counters, plus the full snapshot
region ($1800..$7166) so the JP-marker pointer arrays and DL bytes
match the live binary even after editor activity.

---

## 6. PSID/RSID `.sid` replay container

HVSC ships defMON tunes not as `.prg` editor workfiles but as PSID/RSID
`.sid` *replay* files: a relocatable player plus the tune's data,
produced by defMON's packer. This is a different container from the
`.prg` workfile — there is no `$D6C9` RLE stream. `pydefmon`'s public
reader (`DefmonSong.from_bytes` / `from_file`, `DefmonSidParser.read`)
dispatches on the `PSID`/`RSID` magic and reconstructs the same
`$1800..$7166` runtime image the `.prg` path produces, so
`DefmonPlayer` and every region accessor work identically afterwards.
`DefmonSidParser.recognize()` finds defMON's player signature (the
`$1022` SID-write band — the same anchor sidid keys `DefMon` on), so
`detect()` locates the replay even when it is relocated.

The replay embeds the player's runtime data tables directly rather than
as a saved image, and stores pattern bodies and sidTAB rows *compacted*
(variable length, tightly packed) addressed through pointer tables. The
reader (`pydefmon._sid_format`) re-expands them into the editor's fixed
`$1F00` / `$5F00` strides:

* The runtime data base `DB` sits a fixed `$7DE` bytes above the
  signature site (`$1022 + $7DE = $1800` for a non-relocated player;
  player and data relocate together, so the delta is invariant).
* `DB+$000` / `DB+$100` — per-sidTAB-row pointer lo/hi. Non-zero hi =
  the *absolute* address of that row's compacted body (re-expanded to
  `$5F00 + Y*15`, the row marked active `$11` so `unpacked_snapshot()`
  materialises the pointer); zero hi = a JP source whose lo byte is the
  target row index.
* `DB+$200` / `DB+$280` — per-pattern pointer lo/hi (absolute address
  of the pattern's compacted body, re-expanded to `$1F00 + n*$80`).
* `DB+$300` / `$400` / `$500` — V1/V2/V3 arrangers (verbatim).
* `DB+$600` — per-sidTAB-row DL bytes (verbatim).

**Coverage.** Every DefMon `.sid` in HVSC that sidid identifies (106
tunes as of HVSC #82) is recognised. All but a small set decode to a
structurally sound `DefmonSong`. The exceptions are four Goto80 tunes
(`Evil_Wizard_2`, `Rent-A-Cop`, `Rent-A-Cop_Reloaded`,
`Rent-A-Cop_Reloaded_title`) built with a newer packer variant that
lays its data out compactly behind zero-page-indirect addressing rather
than the absolute-indexed tables above; the reader recognises them but
raises `DefmonError` ("compact/indirect packer variant") rather than
fabricate a song. `tests/test_hvsc_sid_corpus.py` asserts the whole
corpus recognises and that each tune either decodes soundly or raises
that specific error; it `skipTest`s when `$HVSC` is unset.

## 7. Out of scope

* **SID#2 (V3..V5)**: defMON's current SAVE drops SID#2 patterns;
  `pydefmon` is SID#1 only.
* **defMON's `$14EE` startup-RNG cutoff variant**: hardcoded to
  the entropy-bit-clear branch (floor = `$02`, output ASL = NOP).
* **AF column slide direction**: the bit is stored on the voice
  but not separately acted on; the active-slide path reads it
  implicitly through `slide_mode`.
* **Exomizer-packed `.prg` variants**: decompress externally
  (`exomizer`) before handing to `pydefmon.DefmonSong`.
