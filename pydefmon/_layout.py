"""Editor-layout constants and decode steps shared by the replay readers (private).

:mod:`pydefmon._sid_format` (editor-layout packed replays) and
:mod:`pydefmon._goto80_format` (the compact Goto80 runtime) both re-expand a
replay into the editor's ``$1800..$7166`` snapshot; the geometry of that
snapshot, the pattern-stream and sidTAB-row grammar (``streams.lark``) and the
sidTAB row relocation live here.
"""

from __future__ import annotations

from lark import Transformer, v_args
from pysidtracker import BitField, ByteGrammar, Parsed, SidImage

LOAD_ADDRESS = 0x1800
SNAPSHOT_END = 0x7167
SNAPSHOT_SIZE = SNAPSHOT_END - LOAD_ADDRESS  # 22887

# Post-LOAD pass gates on the runtime-state page (see DefmonSong.unpacked_snapshot).
GATE_SONG_POSITION_ADDR = 0x715E
GATE_PAT_BASE_ADDR = 0x715F
GATE_PATTERN_MARKER_ADDR = 0x7162

PATTERN_BANK = 0x1F00
PATTERN_STRIDE = 0x80
SIDTAB_BANK = 0x5F00
SIDTAB_ROW_STRIDE = 15

FLAG_ALT = 0x80
FLAG_GATE_A = 0x40
FLAG_GATE_B = 0x20
FLAG_GATE_N = 0x10
GATE_BITS = (FLAG_GATE_A, FLAG_GATE_B, FLAG_GATE_N)  # payload order: a, b, note

Step = tuple[int, int, int, int]


class EditorPattern(tuple):
    """A pattern's steps, ``(flag, a, b, note)`` each, in the editor's 4-byte form."""

    def to_bytes(self) -> bytes:
        return bytes(b for step in self for b in step)


class PackedPattern(tuple):
    """A pattern's steps in the replay's form: a payload byte per set gate."""

    def to_bytes(self) -> bytes:
        return bytes(
            b
            for flag, *payload in self
            for b in (flag, *(v for gate, v in zip(GATE_BITS, payload) if flag & gate))
        )


#: sidTAB columns per bitmap, in the order the player reads their values.
LOW_COLUMNS = (
    BitField(6, "WGh"),
    BitField(7, "WGl"),
    BitField(5, "AD"),
    BitField(4, "SR"),
    BitField(3, "TR"),
    BitField(2, "AF"),
    BitField(1, "PW"),
)
HIGH_COLUMNS = (
    BitField(7, "PS"),
    BitField(6, "RE"),
    BitField(5, "FV"),
    BitField(4, "CP"),
    BitField(3, "ACID", 2),
)
COLUMNS = tuple(f.tag for f in LOW_COLUMNS + HIGH_COLUMNS)
_TAG_COLUMN = {"T_" + c.upper(): c for c in COLUMNS}


def _tags(fields) -> tuple:
    return tuple(BitField(f.bit, "T_" + f.tag.upper(), f.width) for f in fields)


class RowColumns(tuple):
    """A sidTAB row's ``(low bitmap, high bitmap, ((column, value), ...))``."""

    @classmethod
    def of(cls, columns: dict) -> "RowColumns":
        """The row writing ``columns`` (name -> value), bitmaps holding only their bits."""
        low = sum(1 << f.bit for f in LOW_COLUMNS if f.tag in columns)
        high = sum(1 << f.bit for f in HIGH_COLUMNS if f.tag in columns)
        return cls((low, high, tuple((c, columns[c]) for c in COLUMNS if c in columns)))

    def to_bytes(self) -> bytes:
        low, high, columns = self
        values = dict(columns)
        out = bytearray([low])
        for fields, bitmap in ((LOW_COLUMNS, None), (HIGH_COLUMNS, high)):
            if bitmap is not None:
                out.append(bitmap)
            for f in fields:
                if f.tag in values:
                    out += (values[f.tag] & 0xFFFF).to_bytes(2, "little")[: f.width]
        return bytes(out)


@v_args(inline=True)
class _ToSteps(Transformer):
    # pylint: disable=missing-function-docstring

    def editor_step(self, flag, a, b, note) -> Step:
        return ord(flag), ord(a), ord(b), ord(note)

    def packed_step(self, flag, *payload) -> Step:
        value = ord(flag)
        given = iter(payload)
        a, b, note = (ord(next(given)) if value & g else 0 for g in GATE_BITS)
        return value, a, b, note

    packed_alt = packed_last = packed_step
    editor_last = editor_step

    def editor(self, *items) -> Parsed:
        steps = [i for i in items if isinstance(i, tuple)]
        if len(items) > len(steps):
            steps.append(tuple(ord(t) for t in items[len(steps) :]))
        model = EditorPattern(steps)
        return Parsed(model, len(model.to_bytes()))

    def packed(self, *steps) -> Parsed:
        model = PackedPattern(steps)
        return Parsed(model, len(model.to_bytes()))

    def column(self, tag, *values):
        return _TAG_COLUMN[tag.type], int.from_bytes(bytes(map(ord, values)), "little")

    def low(self, bitmap, *columns):
        return ord(bitmap), columns

    high = low

    def row(self, low, high) -> Parsed:
        model = RowColumns((low[0], high[0], low[1] + high[1]))
        return Parsed(model, len(model.to_bytes()))


#: Pattern streams of both forms (``editor``, ``packed``) and sidTAB rows (``row``).
STREAMS = ByteGrammar(
    __package__,
    "streams.lark",
    ("editor", "packed", "row"),
    _ToSteps(),
    bitmaps={"LOWMAP": _tags(LOW_COLUMNS), "HIGHMAP": _tags(HIGH_COLUMNS)},
)


def store_pattern(snap: bytearray, image: SidImage, pat: int, start: int) -> None:
    """Expand the packed stream at ``start`` into pattern ``pat``'s fixed steps."""
    steps = STREAMS.read(image, start, "packed", f"pattern {pat}").model
    dst = PATTERN_BANK + pat * PATTERN_STRIDE - LOAD_ADDRESS
    body = EditorPattern(steps).to_bytes()
    snap[dst : dst + len(body)] = body


def relocate_sidtab_row(snap: bytearray, image: SidImage, y: int, src: int) -> int:
    """Copy sidTAB row ``y``'s body from ``src`` into its fixed ``$5F00`` slot.

    Returns the slot address (``$5F00 + y * 15``).
    """
    dst = SIDTAB_BANK + y * SIDTAB_ROW_STRIDE
    snap[dst - LOAD_ADDRESS : dst - LOAD_ADDRESS + SIDTAB_ROW_STRIDE] = image.window(
        src, SIDTAB_ROW_STRIDE
    )
    return dst
