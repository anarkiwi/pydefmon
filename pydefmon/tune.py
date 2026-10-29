"""defMON songs as the replay player reads them, in the ``defmon.lark`` grammar.

A :class:`Tune` keeps only what the player consumes from what the editor wrote:
reachable arranger rows, patterns up to their ALT step and the sidTAB rows the
cascade fetches, with JP rows resolved into edges (see ``docs/grammar.md``).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files
from typing import Iterable, Optional, Union

from lark import Lark, Transformer, v_args
from pysidtracker import (
    CodePattern,
    find_code_first,
    init_arguments,
    layout_chains,
    minimise,
    number_chains,
)

from pydefmon._layout import (
    COLUMNS,
    FLAG_ALT,
    FLAG_GATE_A,
    FLAG_GATE_B,
    FLAG_GATE_N,
    GATE_PAT_BASE_ADDR,
    GATE_PATTERN_MARKER_ADDR,
    GATE_SONG_POSITION_ADDR,
    LOAD_ADDRESS,
    PATTERN_BANK,
    PATTERN_STRIDE,
    SIDTAB_BANK,
    SIDTAB_ROW_STRIDE,
    STREAMS,
)
from pydefmon.defmon import STEREO_ENABLE, DefmonError, DefmonSong, SidtabRow

#: V1/V2/V3 arranger pages per SID chip (SID#2 only while stereo is enabled).
ARRANGERS = ((0x1B00, 0x1C00, 0x1D00), (0x6E00, 0x6F00, 0x7000))
JP_TARGET, JP_MARKER, DL = 0x1800, 0x1900, 0x1E00
#: The marker defMON's editor writes on every sidTAB row that is not a JP.
ACTIVE = 0x11
#: The DL bit that stops a row's cascade.
STOP = 0x80
MAX_DURATION = 0x0F
PATTERNS = 0x80
ROWS = 0x100
#: sidTAB columns in the order the player applies them.
_SIGNATURE_FROM_PLAYER_BASE = 0x22
# Jump rows of the players without loop counts: LDA arr2,Y / TAY / LDX arr1,Y.
_UNCOUNTED_JUMPS = CodePattern("BE ?? ?? 10 ?? B9 ?? ?? A8 BE")


@dataclass(frozen=True)
class Play:
    """An arranger row that starts patterns ``voices`` on the chip's voices."""

    voices: tuple[int, int, int]


@dataclass(frozen=True)
class Jump:
    """An arranger row that continues at ``target``.

    With counted jumps a non-zero ``count`` jumps that many times and then
    falls through to the next row; ``0`` always jumps.
    """

    target: int
    count: int = 0


Step = Union[Play, Jump]


@dataclass(frozen=True)
class Event:
    """A pattern step that gates something, ``at`` main ticks into the pattern.

    ``a`` / ``b`` arm sidcall layer 1 / 2 at a row and ``note`` sets the note;
    ``end`` marks the ALT step, ending the arranger step for every voice, and
    is the duration nibble all voices count before the next patterns start.
    """

    at: int
    a: Optional[int] = None
    b: Optional[int] = None
    note: Optional[int] = None
    end: Optional[int] = None


@dataclass(frozen=True)
class Row:
    """A sidTAB row: column writes, then hold ``hold`` frames and fetch ``next``.

    ``hold is None`` stops the cascade on this row, so it has no ``next``.
    """

    columns: tuple[tuple[str, int], ...] = ()
    hold: Optional[int] = None
    next: Optional[int] = None


@dataclass(frozen=True)
class Tune:
    """A defMON song reduced to what its player reads."""

    chips: tuple[dict[int, Step], ...]
    patterns: dict[int, tuple[Event, ...]] = field(default_factory=dict)
    rows: dict[int, Row] = field(default_factory=dict)
    counted: bool = True
    starts: tuple[int, ...] = (0,)
    tempo: Optional[tuple[int, int]] = None
    sid2_base: Optional[int] = None

    @classmethod
    def from_song(cls, song: DefmonSong) -> "Tune":
        """The part of ``song`` its player reads.

        Raises:
            DefmonError: the player would read what the editor never wrote --
            past a pattern's 32 steps, a pattern >= $80, a JP onto a JP row.
        """
        snap = song.unpacked_snapshot()
        mark = _page(song.snapshot, JP_MARKER)
        target = _page(song.snapshot, JP_TARGET)
        dls = _page(snap, DL)

        def resolve(y: int) -> int:
            if mark[y]:
                return y
            t = target[y]
            if not mark[t]:
                raise DefmonError(f"sidTAB row ${y:02x} is a JP onto JP row ${t:02x}")
            return t

        counted = not (
            song.form == "packed"
            and find_code_first(song.image_bytes, _UNCOUNTED_JUMPS, start=0)
        )
        starts = _start_rows(song)
        stereo = bool(song.snapshot[STEREO_ENABLE])
        chips = tuple(
            _walk_arranger(snap, pages, starts, counted)
            for pages in ARRANGERS[: 1 + stereo]
        )
        decoded = {
            p: _decode_pattern(
                snap[_offset(PATTERN_BANK + p * PATTERN_STRIDE) :][:PATTERN_STRIDE],
                resolve,
            )
            for chip in chips
            for step in chip.values()
            if isinstance(step, Play)
            for p in step.voices
        }
        patterns = {}
        for p, horizon in _horizons(chips, decoded).items():
            events, span = decoded[p]
            if span is not None and span <= horizon:
                raise DefmonError(f"pattern ${p:02x} plays past its 32 steps")
            patterns[p] = tuple(ev for ev in events if ev.at <= horizon)
        todo = [y for evs in patterns.values() for ev in evs for y in (ev.a, ev.b)]
        rows: dict[int, Row] = {}
        while todo:
            y = todo.pop()
            if y is None or y in rows:
                continue
            body = bytes(snap[_offset(SIDTAB_BANK + y * SIDTAB_ROW_STRIDE) :][:15])
            columns = tuple(SidtabRow.parse(y, body).values().items())
            if dls[y] & STOP:
                rows[y] = Row(columns)
            else:
                nxt = resolve((y + 1) % ROWS)
                rows[y] = Row(columns, dls[y] + 1, nxt)
                todo.append(nxt)
        timer = song.cia2_timer
        return cls(
            chips=chips,
            patterns=dict(sorted(patterns.items())),
            rows=dict(sorted(rows.items())),
            counted=counted,
            starts=starts,
            tempo=(timer, song.sub_frame_count) if timer else None,
            sid2_base=song.sid2_base if stereo else None,
        )

    def to_song(self) -> DefmonSong:
        """An editor-form :class:`~pydefmon.DefmonSong` that plays this tune.

        Rows keep their numbers when each successor other than the next row
        can take a JP row in the slot after it; otherwise they are laid out
        afresh, chains first.
        """
        song = DefmonSong()
        snap = song.snapshot
        for gate in (
            GATE_SONG_POSITION_ADDR,
            GATE_PAT_BASE_ADDR,
            GATE_PATTERN_MARKER_ADDR,
        ):
            snap[_offset(gate)] = 1
        if self.tempo is not None:
            song.set_cia2_timer(self.tempo[0])
            song.set_sub_frame_count(self.tempo[1])
        if self.sid2_base is not None:
            song.set_stereo(True)
            song.set_sid2_base(self.sid2_base)
        for chip, pages in zip(self.chips, ARRANGERS):
            for y, step in chip.items():
                values = (
                    step.voices
                    if isinstance(step, Play)
                    else (0xFF, step.target, step.count)
                )
                for page, value in zip(pages, values):
                    snap[_offset(page) + y] = value
        slots, jps = _layout(self.rows)
        marker = _offset(JP_MARKER)
        snap[marker : marker + ROWS] = bytes([ACTIVE]) * ROWS
        for y, row in self.rows.items():
            s = slots[y]
            start = _offset(SIDTAB_BANK + s * SIDTAB_ROW_STRIDE)
            snap[start : start + SIDTAB_ROW_STRIDE] = SidtabRow.pack(dict(row.columns))
            snap[_offset(DL) + s] = STOP if row.hold is None else row.hold - 1
        for s, t in jps.items():
            snap[_offset(JP_TARGET) + s] = slots[t]
            snap[marker + s] = 0
        for p, events in self.patterns.items():
            if not 0 <= p < PATTERNS:
                raise DefmonError(f"pattern ${p:02x} is not 0..$7f")
            body = _encode_pattern(events, slots, p)
            start = _offset(PATTERN_BANK + p * PATTERN_STRIDE)
            snap[start : start + len(body)] = body
        return song

    def canonical(self) -> "Tune":
        """This tune with indistinguishable rows / patterns merged and renumbered.

        Rows are minimised as a Moore machine, patterns merged over those row
        classes, then both numbered from 1 by first use (rows chain-consecutive).
        """
        klass = minimise(
            {y: (r.columns, r.hold) for y, r in self.rows.items()},
            {y: r.next for y, r in self.rows.items()},
        )
        pkey = {
            p: tuple(_map_rows(ev, klass) for ev in events if ev != Event(ev.at))
            for p, events in self.patterns.items()
        }
        pnum: dict = {}
        for chip in self.chips:
            for _y, step in sorted(chip.items()):
                if isinstance(step, Play):
                    for p in step.voices:
                        pnum.setdefault(pkey[p], len(pnum) + 1)
        rep = {klass[y]: y for y in sorted(self.rows, reverse=True)}
        succ = {
            k: None if self.rows[y].next is None else klass[self.rows[y].next]
            for k, y in rep.items()
        }
        rnum = number_chains(
            (k for key in pnum for ev in key for k in (ev.a, ev.b)), succ, start=1
        )
        rows = {}
        for k, n in rnum.items():
            row = self.rows[rep[k]]
            nxt = None if row.next is None else rnum[klass[row.next]]
            rows[n] = dataclasses.replace(row, next=nxt)

        def remap(s: Step) -> Step:
            if isinstance(s, Jump):
                return s if self.counted else Jump(s.target)
            return Play(tuple(pnum[pkey[p]] for p in s.voices))

        return dataclasses.replace(
            self,
            chips=tuple({y: remap(s) for y, s in chip.items()} for chip in self.chips),
            patterns={
                n: tuple(_map_rows(ev, rnum) for ev in key) for key, n in pnum.items()
            },
            rows=dict(sorted(rows.items())),
        )

    @classmethod
    def parse(cls, text: str) -> "Tune":
        """Parse ``text`` written in the ``defmon.lark`` grammar."""
        return _ToTune().transform(_parser().parse(text))

    def __str__(self) -> str:
        out = [f"jumps {'counted' if self.counted else 'uncounted'}"]
        out.append("start " + " ".join(_hex(s) for s in self.starts))
        if self.tempo is not None:
            out.append(f"tempo {_hex(self.tempo[0], 4)} x{self.tempo[1]}")
        if self.sid2_base is not None:
            out.append(f"stereo {_hex(self.sid2_base, 4)}")
        for n, chip in enumerate(self.chips, 1):
            out.append(f"sid {n}")
            for y, s in sorted(chip.items()):
                if isinstance(s, Play):
                    body = " ".join(_hex(v) for v in s.voices)
                else:
                    body = f"jump {_hex(s.target)}"
                    body += f" x{s.count}" if self.counted else ""
                out.append(f"  {_hex(y)}: {body}")
        for p, events in self.patterns.items():
            out.append(f"pattern {_hex(p)}")
            for ev in events:
                gates = [
                    f"{name} {_hex(v)}"
                    for name, v in (("a", ev.a), ("b", ev.b), ("n", ev.note))
                    if v is not None
                ]
                if ev.end is not None:
                    gates.append(f"end {ev.end}")
                out.append(f"  {ev.at}: " + " ".join(gates))
        for y, row in self.rows.items():
            given = dict(row.columns)
            cols = [
                f"{c} {'--' if c not in given else _hex(given[c], 4 if c == 'ACID' else 2)}"
                for c in COLUMNS
            ]
            tail = (
                "stop" if row.hold is None else f"hold {row.hold} -> {_hex(row.next)}"
            )
            out.append(" ".join([f"row {_hex(y)}", *cols, tail]))
        return "\n".join(out) + "\n"


def _offset(addr: int) -> int:
    return addr - LOAD_ADDRESS


def _page(snap, addr: int):
    return snap[_offset(addr) :][:0x100]


def _hex(value: int, width: int = 2) -> str:
    return f"${value:0{width}x}"


def _map_rows(ev: Event, rows: dict[int, int]) -> Event:
    return dataclasses.replace(
        ev,
        a=None if ev.a is None else rows[ev.a],
        b=None if ev.b is None else rows[ev.b],
    )


def _start_rows(song: DefmonSong) -> tuple[int, ...]:
    """Arranger row each subtune's init hands the player (``A`` at its base)."""
    if song.form != "packed" or song.signature is None:
        return (0,)
    # pylint: disable-next=import-outside-toplevel,cyclic-import
    from pydefmon.defmon_player import replay_image

    base = song.signature - _SIGNATURE_FROM_PLAYER_BASE
    starts = init_arguments(replay_image(song.image_bytes), base)
    return tuple(dict.fromkeys(s if a is None else a for s, a in enumerate(starts)))


def _walk_arranger(
    snap, pages: tuple[int, int, int], starts: Iterable[int], counted: bool
) -> dict[int, Step]:
    """The rows the arranger walker reaches from ``starts``.

    Only a row stepped onto from the previous one is checked for a jump; the
    row a jump lands on or a counted jump falls through to is played as is.
    """
    v1, v2, v3 = (_page(snap, p) for p in pages)
    steps: dict[int, Step] = {}
    seen = set()
    todo = [(s, True) for s in starts]
    while todo:
        y, checked = todo.pop()
        if (y, checked) in seen:
            continue
        seen.add((y, checked))
        if checked and v1[y] & 0x80:
            step: Step = Jump(v2[y], v3[y] if counted else 0)
            todo.append((step.target, False))
            if step.count:
                todo.append(((y + 1) % ROWS, False))
        else:
            step = Play((v1[y], v2[y], v3[y]))
            if max(step.voices) >= PATTERNS:
                raise DefmonError(f"arranger row ${y:02x} plays {step.voices}")
            todo.append(((y + 1) % ROWS, True))
        steps[y] = step
    return dict(sorted(steps.items()))


def _decode_pattern(body, resolve) -> tuple[tuple[Event, ...], Optional[int]]:
    """Events up to the ALT step, and when a 33rd step would start (no ALT)."""
    events = []
    at = 0
    for flag, a, b, note in STREAMS.parse_prefix(body, "editor").model:
        if flag & (FLAG_ALT | FLAG_GATE_A | FLAG_GATE_B | FLAG_GATE_N):
            events.append(
                Event(
                    at,
                    resolve(a) if flag & FLAG_GATE_A else None,
                    resolve(b) if flag & FLAG_GATE_B else None,
                    note if flag & FLAG_GATE_N else None,
                    flag & MAX_DURATION if flag & FLAG_ALT else None,
                )
            )
        if flag & FLAG_ALT:
            return tuple(events), None
        at += (flag & MAX_DURATION) + 2
    return tuple(events), at


def _end(events: Iterable[Event]) -> Optional[int]:
    return next((ev.at for ev in events if ev.end is not None), None)


def _horizons(chips, decoded) -> dict[int, int]:
    """Per pattern, the latest tick any arranger step lets it play to.

    A step ends at its voices' first ALT, so later events are never applied.
    """
    horizon: dict[int, int] = {}
    for chip in chips:
        for y, step in chip.items():
            if isinstance(step, Jump):
                continue
            ends = [e for p in step.voices if (e := _end(decoded[p][0])) is not None]
            if not ends:
                raise DefmonError(f"arranger row ${y:02x} plays no pattern that ends")
            for p in step.voices:
                horizon[p] = max(horizon.get(p, 0), min(ends))
    return dict(sorted(horizon.items()))


def _split(gap: int) -> list[int]:
    """Duration nibbles of the fewest steps spanning ``gap`` ticks."""
    if gap < 2:
        raise DefmonError(f"pattern events {gap} tick apart; steps last >= 2")
    n = -(-gap // (MAX_DURATION + 2))
    extra = gap - 2 * n
    return [max(0, min(MAX_DURATION, extra - MAX_DURATION * i)) for i in range(n)]


def _encode_pattern(events: tuple[Event, ...], slots: dict[int, int], p: int) -> bytes:
    """Fixed 4-byte steps; an open pattern (no end) pads with the longest steps."""
    if any(ev.end is not None for ev in events[:-1]):
        raise DefmonError(f"pattern ${p:02x} has an end event before its last")
    if not events or events[0].at:
        events = (Event(0),) + events
    out = bytearray()
    for ev, nxt in zip(events, events[1:] + (None,)):
        if nxt is not None:
            durations = _split(nxt.at - ev.at)
        else:
            durations = [MAX_DURATION if ev.end is None else ev.end]
        flag = (
            (FLAG_ALT if ev.end is not None else 0)
            | (FLAG_GATE_A if ev.a is not None else 0)
            | (FLAG_GATE_B if ev.b is not None else 0)
            | (FLAG_GATE_N if ev.note is not None else 0)
        )
        a = 0 if ev.a is None else slots[ev.a]
        b = 0 if ev.b is None else slots[ev.b]
        out += bytes((flag | durations[0], a, b, ev.note or 0))
        for d in durations[1:]:
            out += bytes((d, 0, 0, 0))
    if len(out) > PATTERN_STRIDE:
        raise DefmonError(f"pattern ${p:02x} needs {len(out) // 4} steps (max 32)")
    if events[-1].end is None:
        out += bytes((MAX_DURATION, 0, 0, 0)) * ((PATTERN_STRIDE - len(out)) // 4)
    return bytes(out)


def _layout(rows: dict[int, Row]) -> tuple[dict[int, int], dict[int, int]]:
    """Row slots and JP slots (slot -> row) that give every row its ``next``."""
    jps: dict[int, int] = {}
    if all(0 <= y < ROWS for y in rows):
        for y, row in rows.items():
            s = (y + 1) % ROWS
            if row.next is None or row.next == s:
                continue
            if s in rows or jps.setdefault(s, row.next) != row.next:
                break
        else:
            return {y: y for y in rows}, jps
    return layout_chains(
        {y: r.next for y, r in rows.items()}, ROWS, first=1, error=DefmonError
    )


@lru_cache(maxsize=1)
def _parser() -> Lark:
    grammar = files("pydefmon").joinpath("defmon.lark").read_text(encoding="utf-8")
    return Lark(grammar, parser="lalr")


def _int(token) -> int:
    text = str(token)
    return int(text[1:], 16) if text.startswith("$") else int(text)


@v_args(inline=True)
class _ToTune(Transformer):
    # pylint: disable=missing-function-docstring

    def start(self, *items):
        settings: dict = {}
        chips, patterns, rows = [], {}, {}
        for kind, value in items:
            if kind == "chip":
                chips.append(value)
            elif kind == "pattern":
                patterns[value[0]] = value[1]
            elif kind == "row":
                rows[value[0]] = value[1]
            else:
                settings[kind] = value
        return Tune(chips=tuple(chips), patterns=patterns, rows=rows, **settings)

    def jumps(self, token):
        return "counted", str(token) == "counted"

    def starts(self, *tokens):
        return "starts", tuple(_int(t) for t in tokens)

    def tempo(self, timer, count):
        return "tempo", (_int(timer), _int(count))

    def stereo(self, base):
        return "sid2_base", _int(base)

    def chip(self, _n, *steps):
        return "chip", dict(steps)

    def play(self, y, v1, v2, v3):
        return _int(y), Play((_int(v1), _int(v2), _int(v3)))

    def jump(self, y, target, count=None):
        return _int(y), Jump(_int(target), 0 if count is None else _int(count))

    def pattern(self, p, *events):
        return "pattern", (_int(p), events)

    def event(self, at, *items):
        *gates, end = items
        return Event(_int(at), **dict(gates), end=None if end is None else _int(end))

    def gate_a(self, v):
        return "a", _int(v)

    def gate_b(self, v):
        return "b", _int(v)

    def gate_n(self, v):
        return "note", _int(v)

    def row(self, y, *items):
        *columns, (hold, nxt) = items
        return "row", (_int(y), Row(tuple(c for c in columns if c), hold, nxt))

    def column(self, name, value):
        return None if value == "--" else (str(name), _int(value))

    def hold(self, frames, nxt):
        return _int(frames), _int(nxt)

    def stop(self):
        return None, None
