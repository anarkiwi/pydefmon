# defMON tune format

Canonical reference for the defMON `.prg` file format and runtime RAM layout as
consumed by `pydefmon`.

## Overview

defMON is a C64 tracker ([canonical site](https://defmon.vandervecken.com);
latest release [V20201008](https://csdb.dk/release/?id=196474) on csdb.dk;
see [Versions](#versions) for what `pydefmon` is verified against). `pydefmon`
reads both the `.prg` editor workfile and the PSID/RSID `.sid` replay
container, decoding either to the same `$1800..$7166` structural model (a
`.prg` is the composer's editor form and a `.sid` the packer's canonical form
— see [Editor form vs packer form](#editor-form-vs-packer-form)), and plays a
`.sid` replay frame-accurately.

`DefmonPlayer` plays a `.sid` by running the tune's own relocatable replay on a
jennings 6502; the [oracle test](../tests/test_oracle_hvsc.py) asserts its per-frame
SID-register grid matches the [`sidtrace`](https://github.com/anarkiwi/sidtrace)
`sidplayfp` oracle byte-for-byte over real HVSC tunes. This document is
authoritative for the byte layout the reader/writer (`DefmonSong`) exposes;
tunes are copyright and not redistributed with `pydefmon` -- tests fetch and
cache them on demand.

## Versions

`pydefmon` is verified against **defMON V20201008** on the editor-worktune
path: the `.prg` fixtures (`tests/_support/__init__.py`) ship the
[V20201008](https://csdb.dk/release/?id=196474) release's worktunes, and every
assertion in this document — the `$D6C9` LOAD codec, the `$1800..$7166` RAM
layout, the gated `$CF42`/`$D004`/`$CFB8` post-LOAD passes — derives from that
build. The
HVSC `.sid` corpus additionally spans the Goto80-era (2008) compact-runtime
replay driver, so both `.sid` decode families verify byte-exact — but
worktunes from any other build are not part of the test corpus.

defMON's worktune/replay encoding changed across builds (per the
[defMON changelog](https://defmon.vandervecken.com/doku.php?id=download:download)):

* the 2008 builds save raw `$1700` worktunes and ship the compact-runtime
  replay driver (the Goto80 tunes in HVSC) — see
  [Prehistoric 2008 builds](#prehistoric-2008-builds-raw-1700-worktunes);
* V20140615 added the stereo config;
* V20141108 added the sync delay;
* V20171024 added the post-LOAD `$CFB8` pattern-marker pass;
* V20171026 / V20180211 write larger worktunes.

defMON release series on csdb.dk:

| Build    | CSDb release                                            |
|----------|---------------------------------------------------------|
| V20140612| [150898](https://csdb.dk/release/?id=150898)           |
| V20140615| [150897](https://csdb.dk/release/?id=150897)           |
| V20140711| [150896](https://csdb.dk/release/?id=150896)           |
| V20141108| [150895](https://csdb.dk/release/?id=150895)           |
| V20141111| [159912](https://csdb.dk/release/?id=159912)           |
| V20171024| [159943](https://csdb.dk/release/?id=159943)           |
| V20171026| [160858](https://csdb.dk/release/?id=160858)           |
| V20181027| [170640](https://csdb.dk/release/?id=170640)           |
| V20181101| [170858](https://csdb.dk/release/?id=170858)           |
| V20200601| [191814](https://csdb.dk/release/?id=191814)           |
| V20200918| [195809](https://csdb.dk/release/?id=195809)           |
| V20201008| [196474](https://csdb.dk/release/?id=196474) — the build `pydefmon` tests |

## Container and detection notes

### PRG container

defMON `.prg` tunes are C64 KERNAL-loadable PRG files:

| Field                  | Value                                                       |
|------------------------|-------------------------------------------------------------|
| Load address (header)  | `$1800` (`$00 $18`)                                         |
| Body                   | `$D6C9`-encoded payload of `RAM[$1800..$7166]`              |
| End address            | per-tune, around 4–17 KB                                    |

defMON's LOAD secondary address is `$00`, which forces the load destination to
the SETLFS `(X,Y)` operand (`$1800`) regardless of the file's embedded load
address — so the file always lands at `$1800` even if its 2-byte header says
otherwise.

`pydefmon` reads the file via `DefmonSong.from_file()` and writes via
`DefmonSong.to_file()`. Both go through
[`pydefmon._load_format`](../pydefmon/_load_format.py)'s `$D6C9` codec, which
round-trips byte-for-byte against the real defMON loader (a tune saved by
`to_file()` is loadable by the original binary).

### PSID/RSID `.sid` replay container

HVSC ships defMON tunes not as `.prg` editor workfiles but as PSID/RSID `.sid`
*replay* files: a relocatable player plus the tune's data, produced by defMON's
packer. This is a different container from the `.prg` workfile — there is no
`$D6C9` RLE stream. `pydefmon`'s public reader (`DefmonSong.from_bytes` /
`from_file`, `DefmonSidParser.read`) dispatches on the `PSID`/`RSID` magic and
reconstructs the same `$1800..$7166` *structural model* the `.prg` path
produces, so `DefmonPlayer` and every region accessor apply to both — but the
*content* is the packer's canonical form, not the composer's editor form
(see [Editor form vs packer form](#editor-form-vs-packer-form)).
`DefmonSidParser.recognize()` finds defMON's player signature (the `$1022`
SID-write band — the same anchor sidid keys `DefMon` on), so `detect()` locates
the replay even when it is relocated; the container is unwrapped through the
shared [`pysidtracker`](https://github.com/anarkiwi/pysidtracker) base and header
addresses are not trusted.

### Pack-save export PRGs (SHIFT+P)

defMON's own file menu also exports the packed replay as a bare C64 PRG, in two
forms:

| Form          | Load | Body                                                              |
|---------------|------|-------------------------------------------------------------------|
| `raw`         | `$1000` | the packed replay (player + compaction data), as in the `.sid` image |
| `executable`  | `$0801` | 9-byte BASIC `SYS 2061` stub (bytes `0B 08 01 00 9E 32 30 36 31`) + the editor's host boot block, then the same replay |

`DefmonSong.from_bytes` detects these with `pydefmon._sid_format.prg_replay`
(`find_signature` in the PRG body; the editor workfile is excluded because its
body carries no player with a consistent data base) and decodes the replay
exactly as the PSID/RSID path does, so the structural model, region accessors,
`DefmonPlayer`, and the shared CLI `info`/`reglog`/`wav` all apply.
`DefmonPlayer` synthesizes the init/play addresses from the located player base
(plus the export's tempo, which defMON bakes into the executable's boot) and
renders byte-identically to the matching `.sid`.

The replay embeds the player's runtime data tables directly rather than as a
saved image, and stores pattern bodies and sidTAB rows *compacted* (variable
length, tightly packed) addressed through pointer tables. The reader
(`pydefmon._sid_format`) re-expands them into the editor's fixed `$1F00` /
`$5F00` strides:

* The runtime data base `DB` sits a fixed `$7DE` bytes above the signature site
  (`$1022 + $7DE = $1800` for a non-relocated player; player and data relocate
  together, so the delta is invariant).
* `DB+$000` / `DB+$100` — per-sidTAB-row pointer lo/hi. Non-zero hi = the
  *absolute* address of that row's compacted body (re-expanded to `$5F00 + Y*15`,
  the row marked active `$11` so `unpacked_snapshot()` materialises the pointer);
  zero hi = a JP source whose lo byte is the target row index.
* `DB+$200` / `DB+$280` — per-pattern pointer lo/hi (absolute address of the
  pattern's compacted body, re-expanded to `$1F00 + n*$80`). A compacted body is
  a run of variable-length records — one flag byte followed by one payload byte
  per set gate bit, in gate order `$40` slot_a, `$20` slot_b, `$10` note — ending
  at the `$80` ALT step or after 32 steps. The player walks a record with `Y`
  (`Y=1`, `INY` per set gate) and then advances its stream pointer by exactly
  that many bytes (`$11BD: TYA / CLC / ADC $1186 / STA $1186`), so bodies pack
  back-to-back with no fixed stride.
* `DB+$300` / `$400` / `$500` — V1/V2/V3 arrangers (verbatim).
* `DB+$600` — per-sidTAB-row DL bytes (verbatim).

**Coverage.** Every DefMon `.sid` in HVSC that sidid identifies (106 tunes as of
HVSC #82) is recognised and decodes to a structurally sound `DefmonSong`. The
four Goto80 tunes (`Evil_Wizard_2`, `Rent-A-Cop`, `Rent-A-Cop_Reloaded`,
`Rent-A-Cop_Reloaded_title`) are older builds that store the *same documented*
defMON data model in a more compact runtime encoding — variable-length
control-byte pattern streams behind a pattern-pointer pair, plus relocated sidTAB
bitmask bodies — rather than the absolute-indexed 4-byte-row tables above.
`pydefmon._goto80_format` locates the engine tables relocation-independently (by
the build's own track-stepper / orderlist code idioms) and re-expands them into
the editor-layout snapshot; `from_sid_bytes` uses it as the fallback after the
canonical reconstruction. `tests/test_hvsc_sid_corpus.py` asserts the whole
corpus recognises and decodes soundly, and `tests/test_oracle_hvsc.py`
(`goto80_rentacop`, `goto80_ponky`, `goto80_reloaded_*`) verifies the Goto80
reconstruction frame-exact against the `sidtrace` `sidplayfp` oracle.

### Prehistoric 2008 builds (raw `$1700` worktunes)

The 2008 builds predate the `$D6C9` worktune codec: their editor worktunes are
plain raw PRGs loaded at `$1700`, not the `$1800` RLE body this document
models, so their data layout relative to `$1700` is binary-derived only — no
worktune saved by these builds survives in the wild, and the region map is
unverified. Status per build (from disassembly of the decrunched editors):

| Build | worktune save/LOAD |
|-------|--------------------|
| 56 blocks (`oldversions.zip`) | raw, base `$1700`: LOAD at `$7200..$720B` (secondary lo `$00` / hi `$17`, `JSR $FFD5`), SAVE at `$7238..$724B` (hi `$17` latched, file/drive via the `$7804`/`$7805` pointer vars, `JSR $FFD8`); no RLE codec in the path |
| 60 blocks (Feb/Apr 08) | raw; base via runtime pointer vars `$7727..$772A` (defaulting to `$1700`); layout unverified |
| 72 blocks (Oct 08) | `$1800` + RLE (encoder `$CC54`, decoder `$CC85`) — the modern family; worktunes saved by it decode through the normal path (its zeroed `$1A00` pattern table is the post-LOAD-gate case) |
| 2013 Genesis Project crack | `$1800` + RLE (decoder `$B2F4`, same `$715E`/`$715F`/`$7162` gates) — decoder family matches |

`pydefmon` detects the raw `$1700` container by its PRG load address
(`pydefmon.PREHISTORIC_LOAD_ADDRESS`) and raises the specific
`DefmonParseError` message in `pydefmon.PREHISTORIC_WORKTUNE_ERROR`
("prehistoric 2008 defMON worktune … not supported") instead of the generic
load-address error. The `$1700` base is pinned against the decrunched 56-block
build's KERNAL `LOAD`/`SAVE` calls in
[`tests/test_prehistoric_worktunes.py`](../tests/test_prehistoric_worktunes.py),
which fetches the author-distributed `oldversions.zip` with zip / PRG /
decrunch sha256 pins (and skips offline). Full support would require a
surviving worktune sample to verify the 56/60-block region map against.

## Data model

### Runtime RAM image: `$1800..$7166`

After LOAD + defMON's gated post-LOAD fixup passes (`$CF42` / `$D004` /
`$CFB8`, below), the runtime RAM image is 22887 bytes laid out as:

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
| `$5F00..$6DFF`       | 3840 B  | 256 sidTAB rows × 15 B each                         | `sidtab_region`, `sidtab_row(y)` |
| `$6E00..$6EFF`       | 256 B   | SID#2 voice-4 arranger (stereo)                     | `arranger_v4`          |
| `$6F00..$6FFF`       | 256 B   | SID#2 voice-5 arranger (stereo)                     | `arranger_v5`          |
| `$7000..$70FF`       | 256 B   | SID#2 voice-6 arranger (stereo)                     | `arranger_v6`          |
| `$7100..$7166`       | 103 B   | Runtime-state page (gates, tempo, stereo, SID#2 base, editor state) | `cia2_timer`, `sub_frame_count`, `stereo_enable`, `sid2_base` |

Bytes past `$7166` are editor state and not preserved.

#### Post-LOAD gates (`$715E`, `$715F`, `$7162`)

defMON's `post_load_chain` runs a post-LOAD pass only when its gate byte is
non-zero, and each run pass clears its gate:

| Gate    | Pass    | Effect                                                            |
|---------|---------|-------------------------------------------------------------------|
| `$715E` | `$CF42` | rewrites active rows to the `$5F00 + Y*15` runtime pointers       |
| `$715F` | `$D004` | fills the pattern pointer cache with `$1F00 + X*$80`              |
| `$7162` | `$CFB8` | sets bit 7 of every pattern's step-31 byte (offset `$7C`, ALT)    |

An editor without a pass saves that gate zero, so its LOAD keeps the raw
decoded bytes there (e.g. V20140612 saves `$715F` zero, leaving
`$1A00..$1AFF` zero after LOAD). `DefmonSong.unpacked_snapshot()` models these
gates and `DefmonSong.to_bytes()` keeps whatever gates the source carried.

#### Song-position arrays (`$1800` + `$1900`)

For each song-step `Y` in `0..255`, the cascade decides whether the row is
**active linear** (cascade advances to row `Y+1` after DL underflow) or a **JP
source** (cascade jumps to a different row instead) by reading `$1900,Y`:

* `$1900,Y == $11` — active linear. defMON's `$CF42` post-LOAD pass rewrites both
  bytes: `$1800,Y` becomes the runtime pointer-lo `(SIDTAB_BASE + Y*15) & $FF`,
  `$1900,Y` becomes the pointer-hi `((SIDTAB_BASE + Y*15) >> 8)`. The cascade
  dereferences this pair.
* `$1900,Y == $00` — JP source (or inactive if `$1800,Y == 0` too). `$1800,Y`
  holds the row index the cascade jumps to instead of advancing linearly.
  `$CF42` leaves both bytes alone.

`DefmonSong.set_jp(row, target=None | K)` / `jp_target(row)` are the high-level
edit API; reads return the on-disk marker form, not the runtime-rewritten form.

#### Pattern pointer cache (`$1A00..$1AFF`)

Zero on disk; defMON's `$D004` post-LOAD pass fills it at runtime with the lo/hi
of `$1F00 + X*$80` for `X` in `0..127` (only when gate `$715F` is set — older
edits' saves leave it zero after LOAD). `DefmonSong.unpacked_snapshot()` returns
a copy with this region materialised, matching what the player sees at runtime.

#### Voice arrangers (`$1B00`, `$1C00`, `$1D00` — SID#1)

Per song-step `Y`, the byte at `$1B00,Y` / `$1C00,Y` / `$1D00,Y` is the pattern
number that voice V1/V2/V3 plays on this step. Encoding:

* `$00` — silent voice on this step.
* `$01..$7F` — pattern number (high bit, if set, is an editor-only flag masked
  off by the player).
* Any value with bit 7 set in the V0 slot (`$1B00,Y`) — jump command
  (the `$FF` form defMON writes). `$1C00,Y` becomes the target song step;
  `$1D00,Y` is the repeat count (`0` = unconditional infinite loop).

Edit via `DefmonSong.set_step(y, v1=..., v2=..., v3=...)` for pattern-playing
rows and `DefmonSong.set_jump(y, target=..., count=...)` for jumps.

#### SID#2 stereo arrangers (`$6E00`, `$6F00`, `$7000`)

With stereo enabled (see below), voices 4-6 live on a second SID chip and
carry their own arranger pages, with the identical encoding per song-step `Y`:

* `$6E00,Y` / `$6F00,Y` / `$7000,Y` — V4/V5/V6 pattern numbers.
* A bit-7-set V4 slot (`$6E00,Y`) is that chip's jump command: `$6F00,Y`
  is the target and `$7000,Y` the repeat count.

Each chip's arranger walker is independent, so a stereo song that has a jump
row needs it marked in *both* pages — `DefmonSong.set_jump(y, target, count,
stereo=True)` writes both, and `DefmonSong.set_step(y, ..., v4=..., v5=...,
v6=...)` sets the SID#2 voices (those arguments are opt-in so editing a mono
row never clobbers stereo arranger data). `clear_arranger()` wipes all six
pages. Voices 4-6 share the pattern bank with 1-3; the SID#2 pages are read
only while `stereo_enable` is set, and defMON's own packer rewrites/references
them only in stereo tunes too — `DefmonSong.optimize()` mirrors that gate, so
it never repoints SID#2 references (and never touches a page's jump-row
target/count bytes) on a mono song. Stereo config has been saved in
worktunes since V20140615 ("Stereo SID Returns"); defMON's V20201008 packer
is the first observed to append the three SID#2 arranger pages. No tune in the
defMON release ships with `stereo_enable` set — `.AUTOMATAS2017` does carry
SID#2 arranger data (V4: patterns `1`×8 then a `$FF` jump back to row 0) with
the flag off, i.e. the flag alone decides whether that data is live.

#### Runtime-state page (`$7100..$7166`)

Per-tune playback fields the player reads at runtime (defMON keeps them in
the editor's RAM; a worktune save stores them in the decoded image):

| Address   | Field            | Meaning                                                                 | Accessor                                  |
|-----------|------------------|-------------------------------------------------------------------------|-------------------------------------------|
| `$715A`   | `cia2_timer_lo`  | CIA2 Timer-A reload interval, low byte (per-NMI period)                 | part of `cia2_timer`                      |
| `$715B`   | `cia2_timer_hi`  | CIA2 Timer-A reload interval, high byte                                  | part of `cia2_timer`                      |
| `$715C`   | `sub_frame_count`| NMIs per main player tick: `1`, `2`, `4`, or `8`                          | `sub_frame_count` / `set_sub_frame_count` |
| `$715D`   | `stereo_enable`  | `$00` mono / `$01` stereo (2SID) mode enabled                            | `stereo_enable` / `set_stereo`            |
| `$7164`   | `sid2_base_lo`   | SID#2 register base, low byte (`$20` multiples of `$00..$E0`)           | part of `sid2_base`                       |
| `$7165`   | `sid2_base_hi`   | SID#2 register base, high byte (`$D4/$D5/$DE/$DF`)                       | part of `sid2_base`                       |

The effective main-tick interval is `cia2_timer × sub_frame_count` CPU
cycles. The SID#2 base selects where the second chip's 24 registers are
mapped; the editor cycles it with `CTRL+SHIFT+UP` (hi byte: `$D4 → $D5 →
$DE → $DF`) and `CTRL+C+UP` (lo byte in `$20` steps). The post-LOAD
pass gates sit on the same page (`$715E`, `$715F`, `$7162`, above). All
shipped release worktunes carry live tempo values (e.g. `.DANSA IN` =
`$4CC7` at ×1, `.AUTOMATAS2017` = `$0998` at ×8) with the stereo flag off;
HVSC carries 2SID tunes (e.g. `MUSICIANS/F/F7sus4/Timewarp_Spheres_2SID.sid`,
second SID mapped at `$D5xx`).

defMON stores a *sync delay* ("saved with your tune", since V20141108)
somewhere in this page; its byte offset is not identified, so it is
carried opaquely inside the snapshot rather than given an accessor.

#### sidTAB DL byte (`$1E00..$1EFF`)

Per sidTAB row `Y`:

* `$00..$7F` — hold this row for `dl + 1` frames, then advance per the JP marker.
* `$80..$FF` — STop: cascade freezes; the slot does not advance until a
  `PatternEvent` with `GATE_A` / `GATE_B` re-arms it.

Set via `DefmonSong.set_dl(row, value)`.

#### Pattern bodies (`$1F00..$5EFF`)

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

`PatternEvent.parse(raw)` decodes; factories `note_on()`, `delay()`,
`alt_end()`, `silent_pattern()` build canonical events; read via
`DefmonSong.pattern_events(idx)` and write via `set_pattern_events(idx, events)`.

**Note byte → SID frequency.** The pitch oscillator indexes the 256-entry
`NOTE_PITCH_LO / NOTE_PITCH_HI` tables directly by `note` (not `note-1`). The
player uses plain 8-bit addition for TR shifts, so `current_note` can reach any
value `0..255`. `PatternEvent.sid_freq_word()` returns the same 16-bit value
the player will latch onto `$D400/$D401` (without the per-voice detune;
V0=+0, V1=+1, V2=+2 added by the player on `freq_lo`).

#### sidTAB rows (`$5F00..` × 15 bytes)

Each row is a packed bitmap-of-columns record. The cascade fetches one row per
voice per cascade event and applies the row's columns to that voice's working
state on the frame of the fetch.

```
byte 0           low-half bitmap (which low columns are present)
bytes 1..N       low-half values (in screen-column order)
byte 1+N         high-half bitmap (which high columns are present)
bytes 2+N..M     high-half values (ACID is 2 bytes, little-endian; others 1)
bytes M+1..14    zero pad
```

A column is absent (`None` on the parsed `SidtabRow`) when its bit isn't set in
the bitmap. Column meanings:

| Bitmap | Bit | Column | What it does                                                |
|--------|-----|--------|-------------------------------------------------------------|
| low    | 6   | `WGh`  | Sets `ctrl_main` (voice ctrl: waveform / gate / sync / ring; emitted to `$D404`) |
| low    | 7   | `WGl`  | Sets `ctrl_eor`: XOR mask applied to `ctrl_main` each frame before emit |
| low    | 5   | `AD`   | Sets the voice's attack/decay byte (emitted to `$D405`)     |
| low    | 4   | `SR`   | Sets the voice's sustain/release byte (emitted to `$D406`)  |
| low    | 3   | `TR`   | Sets `current_note`: plain 8-bit add to transpose buffer (= last GATE_N note), no mask, result wraps at $FF |
| low    | 2   | `AF`   | Sets `slide_mode`: `$00` = no slide, `$01..$7F` = portamento, `$80..$FF` = active slide |
| low    | 1   | `PW`   | Sets `pulse_hi = byte`, `pulse_lo = byte & $F0` (12-bit `$YX` PW encoding; emitted to `$D402/$D403`) |
| high   | 7   | `PS`   | Sets `ps_depth`: pulse-width sweep depth + direction (bit 7 = direction) |
| high   | 6   | `RE`   | Sets `$D417` resonance + voice-routing (3-way dispatch on byte value) |
| high   | 5   | `FV`   | Sets `$D418` filter mode + volume (volume always emitted as `$0F`) |
| high   | 4   | `CP`   | Sets cutoff-slide saturation-step extra (added to `$D416` each frame) |
| high   | 3   | `ACID` | 16-bit cutoff-slide command, stored little-endian (step byte first, then control: bit 7 = slide-vs-absolute, bit 6 = SBC vs ADC) |

`SidtabRow.parse(index, raw, *, jp=None, dl=None)` decodes the row;
`pack({col: val})` / `to_bytes()` go back. Walk a cascade from row `Y` with
`DefmonSong.sidcall_frames(Y)` — returns a `list[SidcallFrame]` terminating at
the first STop, JP loopback, or `max_frames`.

## Editor form vs packer form

Both the `.prg` editor workfile and a `.sid` replay decode to the *same
structural model* — the 22887-byte `$1800..$7166` editor-layout snapshot — so
every region accessor, the edit API, and `unpacked_snapshot()` apply to both.
But for the *same tune* the two are **different content forms** and are **not
byte-identical**, so do not assert `.prg.unpacked_snapshot() ==
sid.unpacked_snapshot()`:

* the **editor form** (`.prg`) is exactly what the composer last saved: original
  pattern and sidTAB-row numbering, one event per step as authored, and the
  `$715A..$715C` tempo bytes populated.
* the **packed form** (`.sid`) is defMON's `$D02C`-packer output: patterns and
  sidTAB rows are deduped and renumbered, runs of empty steps are folded into
  longer durations, and the `$715A..$715C` tempo is zeroed (the replay cadence
  comes from the player's interrupt programming, not from `$715A`).

These are isomorphic, semantics-preserving rearrangements — both describe the
same music. `DefmonSong.form` reports which form a song holds
(`"editor"` / `"packed"`), and `DefmonSong.diff(other)` gives a region-wise
comparison in `DIFF_REGIONS` order: for a `.prg` against its matching `.sid`
it reports which regions agree and which differ, instead of a false whole-image
`==`. Musical equivalence (the byte-exact rendered SID stream) is verified
separately by the oracle tests, not by snapshot comparison.

## Player and playback notes

`DefmonPlayer` does not model any of the following in Python — it runs the
tune's own replay on a jennings 6502, so all of it happens as real machine code.
These notes document what defMON's replay does, for reference. The play cadence
is a tune-specific CIA-2 Timer-A NMI with reload `(($715A | $715B << 8)) * $715C`
cycles (`$715A/$715B` = per-NMI cycle count, `$715C` = sub-frame ratio), firing
`sub_frame_count` NMIs per main player tick. The worktune carries those fields
in the runtime-state page (exposed as `DefmonSong.cia2_timer` /
`DefmonSong.sub_frame_count` / `DefmonSong.stereo_enable` /
`DefmonSong.sid2_base`; see the [runtime-state page](#runtime-state-page-71007166)
section).

* **All NMIs** run the SID-write band (24 writes), the global `$D417`/`$D418`/
  `$D416` writes, the ACID cutoff slide, the sidTAB cascade arms for both layers
  × 3 voices, the pitch-slide oscillator, and the pulse-width sweep.
* **Main-tick NMIs** (every `$715C`-th) additionally run the arranger walk +
  per-voice pattern advance, BEFORE the cascade.

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

`CTRL` is emitted as `ctrl_main XOR ctrl_eor` (so the sidTAB `WGl` column flicks
waveform/gate bits each frame).

### Sidcall cascade

Per voice × layer, the cascade tracks `(row_idx, step_counter)`:

* `counter & $80` set → frozen / STop; no advance.
* `counter == 0` → fetch row at `row_idx`, apply via the sidTAB bitmap walk,
  store `$1E00,row_idx` (DL byte) into the counter, advance `row_idx` to
  `(effective_row + 1) & $FF`.
* `counter in $01..$7F` → decrement, no fetch.

A pattern `GATE_A` / `GATE_B` re-arms by setting the layer's `row_idx` and
resetting `counter := 0`. A JP redirect at fetch time (`$1900,row_idx == 0`)
substitutes the target row from `$1800,row_idx` and the cascade resumes linearly
from `target + 1`.

### Pitch slide

Per voice, `slide_mode` selects the path:

* `0` — no slide. `freq = NOTE_PITCH[note] + pitch_base` (per-voice detune V0=0,
  V1=1, V2=2 on `freq_lo`; carry NOT propagated to `freq_hi`).
* `$01..$7F` — portamento toward `(note + slide_mode)`. Adds a one-frame
  adjacent-semitone step; not integrated across frames.
* `$80..$FF` — active slide. Index a per-mode rate LUT, integrate into a 16-bit
  accumulator, then add to `NOTE_PITCH[note]`.

`GATE_N` from a pattern step zeros the slide accumulator and the slide mode, so a
fresh note never inherits the prior row's slide.

### Pulse-width sweep (PS)

`ps_depth` (set via sidTAB `PS`) modulates `pulse_lo` each frame:

* `$00` — no-op.
* Bit 7 set (`$80..$FF`) — ADD path. `pulse_lo += (depth & $7F)` with carry into
  `pulse_hi`. On `pulse_hi == $0F` overflow, clamp `pulse_lo = $F8` and flip
  direction (XOR `depth ^= $80`).
* Bit 7 clear (`$01..$7F`) — SUB path. `pulse_lo -= depth - 1` (the `-1` comes
  from defMON's uncontrolled carry-in at the SBC). On `pulse_hi == 0` borrow,
  clamp `pulse_lo = $01` and flip direction.

Because the `PW` column only writes the high nibble of `pulse_lo` (`byte & $F0`),
tunes can't directly set `pulse_lo`'s low nibble — the PS sweep is the only way
to access those bits.

### ACID cutoff slide

A 16-bit accumulator (lo / hi) integrates a signed step per NMI:

* Lo byte: `acc_lo = ADC/SBC(acc_lo, step_lo, carry_in=0)`. The CLC-then-SBC
  idiom subtracts an extra 1, so SBC effectively does `acc_lo - step_lo - 1`.
* Hi byte: `acc_hi = ADC/SBC(acc_hi, step_hi, carry_from_lo)`. If the result has
  bit 7 set, reload `acc_hi = floor` (saturation clamp).
* Output: `A = acc_hi + cutoff_extra + carry_from_hi`. If `A & $80` or `A < $02`,
  output `floor` instead. Optional ASL doubles output. Written to `$D416`.

The `ACID` sidTAB column carries a 16-bit `(low, high)` command, stored in the
row as `low` then `high` — the order the player reads them (`$176D: LAX ($FB),Y`
→ X = `low`, `INY` / `LDA ($FB),Y` → A = `high`, then `$1772: BMI`):

* `high & $80` clear → absolute reset: `acc_lo := low`, `acc_hi := high`,
  `step_lo/hi := 0`.
* `high & $80` set → slide configuration: `step_lo := low`. If `high & $40` set,
  opcode := SBC (subtract) and `step_hi := high & $3F`. Else opcode := ADC (add)
  and `step_hi := high & $7F`.

The `CP` sidTAB column sets the cutoff-extra add directly.

### Oracle scope

The oracle test verifies byte-exact rendering across both replay-decode families
(standard editor-layout and Goto80 compact-runtime), including CIA multi-speed
tunes and tunes whose cutoff seeds from the V3 oscillator readback
(`$D41B/$D41C`). `DefmonPlayer` calls play at the tune's play-routine cadence and
answers those reads from the SID model the PSID header names, as `sidplayfp`
does.

## References

- [defMON](https://defmon.vandervecken.com) — canonical site; latest tested
  release [V20201008](https://csdb.dk/release/?id=196474) on csdb.dk;
  [changelog](https://defmon.vandervecken.com/doku.php?id=download:download).
- [`sidtrace`](https://github.com/anarkiwi/sidtrace) — the `sidplayfp` register
  oracle the player is checked against; see
  [`tests/test_oracle_hvsc.py`](../tests/test_oracle_hvsc.py).
- [`pysidtracker`](https://github.com/anarkiwi/pysidtracker) — shared
  container/image/detection/player base.
