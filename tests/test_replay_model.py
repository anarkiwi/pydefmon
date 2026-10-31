"""``Tune.from_replay``: the song as the replay reads it from the memory its init leaves.

The model is held to ``specs/defmon.play`` run by ``DefmonPlayer``, call by call: the rows
the walker loads, the steps each reader applies and the rows each cascade block fetches.
"""

import pytest

from pysidtracker import song_frames
from pysidtracker.testing import resolve_tune

from pydefmon import DefmonError, DefmonPlayer, DefmonSong, Event, Jump, Play, Row, Tune
from pydefmon._layout import STREAMS
from pydefmon.spec import (
    READER_STRIDE,
    VOICE_STRIDE,
    build_facts,
    entry_base,
    player_bases,
)
from pydefmon.tune import Init

from tests._support import corpus_relpaths

CALLS = 2500
BLOCKS = 6
GOTO80 = "MUSICIANS/G/Goto80/"
RENT_A_COP = GOTO80 + "Rent-A-Cop.sid"
RELOADED = GOTO80 + "Rent-A-Cop_Reloaded.sid"
RELOADED_TITLE = GOTO80 + "Rent-A-Cop_Reloaded_title.sid"
TECHNO_AHA = GOTO80 + "Techno_Aha.sid"
IN_MEMORY_OF = GOTO80 + "In_Memory_of.sid"
GLOW_WORM = "MUSICIANS/I/Ilkke/Glow_Worm.sid"
VACUOLE = "MUSICIANS/I/Ilkke/Vacuole.sid"
REDUCED = (
    "MUSICIANS/F/F7sus4/Triangle_My_Amelie.sid",
    GOTO80 + "Svenska_Narkotika_3.sid",
    "MUSICIANS/N/NecroPolo/Anticitizen_64.sid",
)
NAMED = (
    [(RENT_A_COP, s) for s in range(1, 7)]
    + [(RELOADED, s) for s in range(1, 11)]
    + [(RELOADED_TITLE, 1), (TECHNO_AHA, 1), (IN_MEMORY_OF, 1)]
)
PLAYED = [(rel, None) for rel in corpus_relpaths() if rel not in REDUCED] + NAMED


def _data(relpath: str) -> bytes:
    path = resolve_tune(relpath)
    if path is None:
        pytest.skip(f"tune {relpath} unavailable (offline, not cached)")
    return path.read_bytes()


def _player(relpath: str, song=None) -> DefmonPlayer:
    return DefmonPlayer(_data(relpath), subtune=None if song is None else song - 1)


def _facts(player):
    mem = player.memory
    return mem, build_facts(mem, player_bases(mem)[0], player.entry)


class Patched:
    """A real player's post-init memory with song tables written over, each cell once."""

    def __init__(self, relpath: str, song=None):
        player = _player(relpath, song)
        self.entry = player.entry
        self.mem = bytearray(player.memory)
        self.facts = build_facts(self.mem, player_bases(self.mem)[0], self.entry)
        self.free, self.written = 0xC000, set()
        self.mem[self.facts.binding["cursor"]] = 0

    def cell(self, name: str, index: int) -> int:
        at = self.facts.tables[name] + index
        assert at not in self.written, f"{name}[{index}] is another table's cell"
        self.written.add(at)
        return at

    def table(self, name: str, index: int, value: int) -> None:
        self.mem[self.cell(name, index)] = value

    def put(self, data: bytes) -> int:
        at, self.free = self.free, self.free + len(data)
        self.mem[at : at + len(data)] = data
        return at

    def pattern(self, p: int, data: bytes, at=None) -> None:
        at = self.put(data) if at is None else at
        self.table("pat_lo", p, at & 0xFF)
        self.table("pat_hi", p, at >> 8)

    def arranger(self, y: int, *voices: int) -> None:
        for name, value in zip(("v1", "v2", "v3"), voices):
            self.mem[self.cell(name, y)] = value

    def row(self, y: int, at: int, dl: int = 0) -> None:
        self.table("rows_lo", y, at & 0xFF)
        self.table("rows_hi", y, at >> 8)
        self.table("dl", y, dl)

    def tune(self) -> Tune:
        return Tune.from_replay(bytes(self.mem), entry=self.entry)


def _armed(patched: Patched, row: int) -> Tune:
    """The tune whose one pattern arms ``row`` at layer A and then ends."""
    patched.arranger(0, 9, 9, 9)
    patched.arranger(1, 0x80, 0, 0)
    patched.pattern(9, bytes((0xC0, row)))
    return patched.tune()


def test_the_replay_streams_have_no_step_cap_and_the_editor_streams_have():
    data = b"\x00" * 40 + b"\x80\xff"
    assert len(STREAMS.parse_prefix(data, "stream").model) == 41
    assert STREAMS.parse_prefix(data, "stream").size == 41
    assert len(STREAMS.parse_prefix(data, "packed").model) == 32
    fixed = b"\x00\x00\x00\x00" * 40 + b"\x80\x01\x02\x03"
    assert len(STREAMS.parse_prefix(fixed, "fixed").model) == 41
    assert len(STREAMS.parse_prefix(fixed, "editor").model) == 32


def test_a_stream_reads_a_payload_byte_a_gate_and_a_fixed_stream_three():
    model = STREAMS.parse_prefix(
        b"\x70\x01\x02\x03\xf0\x04\x05\x06\x07", "stream"
    ).model
    assert tuple(model) == ((0x70, 1, 2, 3), (0xF0, 4, 5, 6))
    model = STREAMS.parse_prefix(b"\x10\x00\x00\x09\xc0\x07\x00\x00", "fixed").model
    assert tuple(model) == ((0x10, 0, 0, 9), (0xC0, 7, 0, 0))


def test_a_stream_with_no_alt_step_is_not_a_stream():
    with pytest.raises(STREAMS.error):
        STREAMS.parse_prefix(b"\x00" * 100, "stream")


@pytest.mark.parametrize("relpath", [RENT_A_COP, RELOADED, RELOADED_TITLE, TECHNO_AHA])
def test_the_tunes_the_snapshot_decode_refuses_are_read_from_memory(relpath):
    data = _data(relpath)
    with pytest.raises(DefmonError):
        Tune.from_song(DefmonSong.from_bytes(data))
    tune = Tune.from_player(data)
    assert tune.patterns and tune.rows and tune.init


def test_a_pattern_runs_to_its_alt_step_past_the_32_steps_of_the_editor():
    mem, facts = _facts(_player(RENT_A_COP, 5))
    steps = []
    for p in Tune.from_replay(mem).patterns:
        at = mem[facts.tables["pat_lo"] + p] | mem[facts.tables["pat_hi"] + p] << 8
        steps.append(len(STREAMS.parse_prefix(mem[at:] + mem[:at], "stream").model))
    assert max(steps) > 32


def test_a_fixed_build_reads_past_32_steps_to_the_first_flag_with_bit_7():
    patched = Patched(TECHNO_AHA)
    assert patched.facts.flags["fixed"]
    patched.arranger(0, 5, 5, 5)
    patched.arranger(1, 0x80, 0, 0)
    patched.pattern(5, b"\x01\x00\x00\x00" * 40 + b"\xc3\x07\x00\x00")
    tables, mem = patched.facts.tables, patched.mem
    eff = 7 if mem[tables["rows_hi"] + 7] else mem[tables["rows_lo"] + 7]
    assert patched.tune().patterns[5] == (Event(120, eff, None, None, 3),)


def test_a_pattern_continues_into_the_memory_after_it_and_around_the_top():
    patched = Patched(GLOW_WORM)
    assert not patched.facts.flags["fixed"]
    patched.arranger(0, 9, 9, 9)
    patched.arranger(1, 0x80, 0, 0)
    patched.mem[0xFFFD:] = b"\x12\x05\x00"
    patched.mem[0] = 0x80
    patched.pattern(9, b"", at=0xFFFD)
    assert patched.tune().patterns[9] == (Event(0, note=5), Event(6, end=0))


def test_a_pattern_with_no_alt_step_in_64_kib_is_refused(monkeypatch):
    patched = Patched(GLOW_WORM)
    real = STREAMS.parse_prefix

    def refuse(data, rule):
        if rule == "stream":
            raise STREAMS.error("no ALT step")
        return real(data, rule)

    monkeypatch.setattr(STREAMS, "parse_prefix", refuse)
    with pytest.raises(DefmonError, match="ALT"):
        _armed(patched, 1)


def test_a_pattern_number_of_128_or_more_indexes_the_pointer_tables_past_the_editors():
    patched = Patched(GLOW_WORM)
    patched.arranger(0, 5, 130, 255)
    patched.arranger(1, 0x80, 0, 0)
    patched.pattern(5, b"\x80")
    patched.pattern(130, b"\x91\x07")
    patched.pattern(255, b"\x92\x08")
    tune = patched.tune()
    assert tune.chips[0][0] == Play((5, 130, 255))
    assert tune.patterns[130] == (Event(0, note=7, end=1),)
    assert tune.patterns[255] == (Event(0, note=8, end=2),)
    with pytest.raises(DefmonError):
        tune.to_song()


def test_a_jp_row_names_its_target_and_the_row_after_a_row_is_the_target_of_a_jp():
    patched = Patched(GLOW_WORM)
    patched.row(40, patched.put(b"\x20\x09\x00"), dl=3)
    patched.row(41, patched.put(b"\x00\x00"), dl=0x80)
    patched.row(50, patched.put(b"\x20\x05\x00"), dl=1)
    patched.row(51, 40)
    tune = _armed(patched, 50)
    assert tune.rows[50] == Row((("AD", 5),), 2, 40)
    assert tune.rows[40] == Row((("AD", 9),), 4, 41)
    assert tune.rows[41] == Row()
    patched = Patched(GLOW_WORM)
    patched.row(40, patched.put(b"\x20\x09\x00"), dl=3)
    patched.row(41, 40)
    assert _armed(patched, 41).patterns[9][0].a == 40


def _zero_page(patched: Patched) -> int:
    cells = {patched.facts.binding[n] for n in ("rowptr_lo", "rowptr_hi", "scratch")}
    return next(a for a in range(0x20, 0xE0, 0x10) if not cells & set(range(a, a + 15)))


def test_a_jp_row_whose_target_is_a_jp_row_is_read_from_page_zero():
    patched = Patched(GLOW_WORM)
    at = _zero_page(patched)
    patched.mem[at : at + 3] = b"\x20\x07\x00"
    patched.row(60, at, dl=2)
    patched.row(61, 60)
    tune = _armed(patched, 61)
    assert tune.patterns[9][0].a == 60
    assert tune.rows[60] == Row((("AD", 7),), 3, tune.rows[60].next)


def test_a_page_zero_row_over_the_cells_the_row_interpreter_writes_is_refused():
    patched = Patched(GLOW_WORM)
    cell = patched.facts.binding["rowptr_lo"]
    patched.row(60, cell - 1)
    patched.row(61, 60)
    with pytest.raises(DefmonError, match="writes"):
        _armed(patched, 61)


def test_a_row_the_walker_lands_on_is_played_as_patterns_whatever_its_v1():
    patched = Patched(VACUOLE)
    assert not patched.facts.flags["uncounted"]
    rows = {
        0: (3, 4, 5),
        1: (0x81, 6, 2),
        2: (0x90, 7, 8),
        3: (0x91, 6, 9),
        4: (3, 4, 5),
        5: (0x80, 0, 0),
        6: (1, 1, 1),
        7: (0x80, 3, 5),
        8: (1, 1, 1),
        9: (0x80, 0, 0),
    }
    for y, voices in rows.items():
        patched.arranger(y, *voices)
    for p in (3, 4, 5, 144, 145, 7, 8, 1, 6, 9):
        patched.pattern(p, b"\x80")
    tune = patched.tune()
    assert tune.chips[0][1] == Jump(6, 2)
    assert tune.chips[0][3] == Jump(6, 9)
    assert tune.chips[0][2] == Play((144, 7, 8))
    assert tune.landed == ({2: Play((144, 7, 8)), 3: Play((145, 6, 9))},)
    assert Tune.parse(str(tune)) == tune
    canon = tune.canonical()
    assert len(canon.landed[0]) == 2 and Tune.parse(str(canon)) == canon
    with pytest.raises(DefmonError):
        tune.to_song()


def test_the_state_init_leaves_is_exposed():
    mem, facts = _facts(_player(VACUOLE))
    init = Tune.from_replay(mem).init
    assert isinstance(init, Init)
    assert (init.owner, init.count) == tuple(
        mem[facts.state[n]] for n in ("owner", "count")
    )
    assert init.cursor == mem[facts.state["cursor"]]
    assert init.trigger == mem[facts.state["trigger"]] and init.trigger & 0x80
    assert init.voices[1]["detune"] == mem[facts.state["detune"] + VOICE_STRIDE]
    assert init.voices[2]["counter"] == mem[facts.state["counter"] + 2 * READER_STRIDE]
    assert set(init.cells) == {
        "cut_lo",
        "cut_hi",
        "cut_step_lo",
        "cut_step_hi",
        "res",
        "fv",
    }


def test_an_uncounted_build_has_no_loop_state_and_no_counted_jumps():
    mem, facts = _facts(_player(RENT_A_COP, 1))
    assert facts.flags["uncounted"]
    tune = Tune.from_replay(mem)
    assert not tune.counted and (tune.init.owner, tune.init.count) == (0, 0)
    assert all(not s.count for s in tune.chips[0].values() if isinstance(s, Jump))


@pytest.mark.parametrize("relpath", REDUCED)
def test_the_reduced_entry_runs_no_arranger(relpath):
    with pytest.raises(DefmonError):
        Tune.from_player(_data(relpath))


def test_memory_with_no_player_is_refused():
    with pytest.raises(DefmonError):
        Tune.from_replay(bytes(0x10000))
    with pytest.raises(DefmonError):
        Tune.from_replay(b"\x00")


def test_a_tune_with_landings_round_trips_its_text():
    tune = Tune(
        chips=({0: Play((1, 1, 1)), 1: Jump(0, 2)},),
        landed=({1: Play((2, 2, 2))},),
        patterns={1: (Event(0, end=0),), 2: (Event(0, end=0),)},
    )
    assert Tune.parse(str(tune)) == tune


class Observer:
    """The walker's loads, the readers' steps and the cascade blocks' state, call by call."""

    def __init__(self, player, facts):
        self.player, self.facts = player, facts
        self.reduced, self.calls = {}, 0
        self.loads, self.blocks, self.voices, self.rowptr = [], [], [], []
        self.full_at = []
        self.applies = [[], [], []]

    def full(self, entry) -> bool:
        if entry not in self.reduced:
            facts = build_facts(self.player.memory, self.facts.base, entry)
            self.reduced[entry] = facts.flags["reduced"]
        return not self.reduced[entry]

    def at(self, mem, name, index=0, stride=VOICE_STRIDE):
        return mem[self.facts.binding[name] + index * stride]

    def call(self, entry, pre, post) -> None:
        """One spec call of this copy of the player."""
        if self.full(entry):
            self.observe(pre, post)
            self.full_at.append(len(self.blocks))
            self.calls += 1
        self.record(post)

    def observe(self, pre, post) -> None:
        at, c = self.at, self.calls
        loading = at(pre, "trigger") & 0x80
        if loading:
            row = (at(post, "cursor") - 1) & 0xFF
            self.loads.append((c, row, at(pre, "trigger") & 15))
        for x in range(3):
            flag = at(pre, "flag", x, READER_STRIDE)
            waiting = not at(pre, "counter", x, READER_STRIDE) & 0x80
            if loading or waiting or not flag & 0xF0:
                continue
            a = (at(post, "blk_rowidx", x) - 1) & 0xFF if flag & 0x40 else None
            b = (at(post, "blk_rowidx", 3 + x) - 1) & 0xFF if flag & 0x20 else None
            note = at(post, "note_base", x) if flag & 0x10 else None
            self.applies[x].append((c, a, b, note, flag & 15 if flag & 0x80 else None))

    def record(self, post) -> None:
        """The cascade blocks and voice records after a call, full or not."""
        at = self.at
        self.blocks.append(
            [
                (at(post, "blk_rowidx", k), at(post, "blk_counter", k))
                for k in range(BLOCKS)
            ]
        )
        self.voices.append(
            {n: [at(post, n, v) for v in range(3)] for n in ("wgh", "wgl", "ad", "sr")}
        )
        self.rowptr.append(at(post, "rowptr_lo") | at(post, "rowptr_hi") << 8)


def watch(player, observers, frames):
    """Play ``frames`` frames, or until ``CALLS`` full calls, each spec call observed."""
    run = player._frame_of  # pylint: disable=protected-access

    def observed(entry):
        pre = player.memory
        run(entry)
        post = player.memory
        for observer in observers:
            if observer.facts.base == entry_base(pre, entry):
                observer.call(entry, pre, post)

    player._frame_of = observed  # pylint: disable=protected-access
    for _ in range(frames):
        player.play_frame()
        if min(o.calls for o in observers) >= CALLS:
            break


def predicted_phrases(tune, last):
    """``(call, row, delay, events per voice, span)`` of each phrase loaded before ``last``."""
    chip = tune.chips[0]
    landed = tune.landed[0] if tune.landed else {}
    y, owner, left = tune.init.cursor, tune.init.owner, tune.init.count
    call, delay = 0, tune.init.trigger & 15
    while call < last:
        step = chip[y]
        if isinstance(step, Jump):
            if not step.count:
                y = step.target
            elif owner != y:
                owner, left, y = y, step.count, step.target
            else:
                left = (left - 1) & 0xFF
                if left:
                    y = step.target
                else:
                    owner, y = 0, (y + 1) & 0xFF
            step = landed.get(y) or chip[y]
        events = [tune.patterns[p] for p in step.voices]
        ends = [
            (ev.at, x, ev.end)
            for x, evs in enumerate(events)
            for ev in evs
            if ev.end is not None
        ]
        span = min(at for at, _, _ in ends)
        yield call, y, delay, events, span
        call += delay + span + 2
        delay = max((x, end) for at, x, end in ends if at == span)[1]
        y = (y + 1) & 0xFF


def check_rows(tune, observer, phrases, start_of):
    """The cascade blocks fetch the model's rows, with its holds and successors."""
    arms = {}
    for start, _, delay, events, span in phrases:
        for x, evs in enumerate(events):
            for ev in (e for e in evs if e.at <= span):
                for k, row in ((x, ev.a), (3 + x, ev.b)):
                    if row is not None:
                        arms.setdefault(start + delay + 1 + ev.at, {})[k] = row
    counter, cursor, total = [None] * BLOCKS, [None] * BLOCKS, 0
    arms = {observer.full_at[c]: arm for c, arm in arms.items() if c < observer.calls}
    for c in range(len(observer.blocks)):
        for k, row in arms.get(c, {}).items():
            counter[k], cursor[k] = 0, row
        fetched = []
        for k in range(BLOCKS):
            if counter[k] is None:
                continue
            index, count = observer.blocks[c][k]
            if counter[k]:
                counter[k] -= 1
            else:
                row = tune.rows[cursor[k]]
                assert index == (cursor[k] + 1) & 0xFF, (c, k)
                fetched.append((k, cursor[k], row))
                counter[k], cursor[k] = (
                    (None, None) if row.hold is None else (row.hold - 1, row.next)
                )
            assert count & 0x80 if counter[k] is None else count == counter[k], (c, k)
        if fetched:
            total += len(fetched)
            assert observer.rowptr[c] == start_of(fetched[-1][1])
            for name, cell in (
                ("WGh", "wgh"),
                ("WGl", "wgl"),
                ("AD", "ad"),
                ("SR", "sr"),
            ):
                for v in range(3):
                    written = [
                        dict(r.columns)[name]
                        for k, _, r in fetched
                        if k % 3 == v and name in dict(r.columns)
                    ]
                    if written:
                        assert observer.voices[c][cell][v] == written[-1], (c, name, v)
    assert total


@pytest.mark.parametrize(
    "relpath, song", PLAYED, ids=[f"{r.split('/')[-1]}:{s}" for r, s in PLAYED]
)
def test_the_model_reproduces_the_spec_players_walk_steps_and_rows(relpath, song):
    data = _data(relpath)
    player = DefmonPlayer(data, subtune=None if song is None else song - 1)
    mem = player.memory
    bases = player_bases(mem)
    models = [(b, Tune.from_replay(mem, base=b), build_facts(mem, b)) for b in bases]
    observers = [Observer(player, facts) for _, _, facts in models]
    watch(player, observers, song_frames(data, song))
    for (_, tune, facts), observer in zip(models, observers):
        check(tune, facts, mem, observer)


def check(tune, facts, mem, observer):
    """The walk, the steps and the rows of ``tune`` are those ``observer`` saw."""
    assert all(v["count_a"] & v["count_b"] & 0x80 for v in tune.init.voices)
    tables = facts.tables

    def start_of(y):
        return mem[tables["rows_hi"] + y] << 8 | mem[tables["rows_lo"] + y]

    phrases = list(predicted_phrases(tune, observer.calls))
    assert observer.loads == [(c, y, t) for c, y, t, *_ in phrases]
    for x in range(3):
        want = [
            (start + delay + 1 + ev.at, ev.a, ev.b, ev.note, ev.end)
            for start, _, delay, events, span in phrases
            for ev in events[x]
            if ev.at <= span
        ]
        assert observer.applies[x] == [w for w in want if w[0] < observer.calls]
    check_rows(tune, observer, phrases, start_of)
