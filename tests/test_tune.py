"""The ``defmon.lark`` song grammar: decoding, text, canonical form and writing.

Synthetic songs are built with the :class:`DefmonSong` edit API and checked
against the player's read rules in ``docs/grammar.md``. Fixture tests use the
defMON release worktunes and the HVSC corpus and skip when unreachable.
"""

import dataclasses
import unittest

from pysidtracker.facets import Entry, detected

from pydefmon import DefmonSong, Event, Jump, PatternEvent, Play, Row, SidtabRow, Tune
from pydefmon._layout import LOAD_ADDRESS, SIDTAB_BANK, SIDTAB_ROW_STRIDE
from pydefmon._worktune_oracle import editor_replay
from pydefmon.defmon import DefmonError
from pydefmon.defmon_player import DefmonPlayer
from pydefmon.tune import ACTIVE, JP_MARKER, JP_TARGET, _layout

from tests._support import editor_image_bytes, fixture_path, resolve_corpus
from tests.test_worktune_replay import _WORKTUNES

_RENDER_FRAMES = 600
_SOUND_TUNES = ("glow_worm.prg", "matsam0t.prg")
_PACKER_TUNES = (
    "glow_worm.prg",
    "dansa_in.prg",
    "hive_machines.prg",
    "radio_robon95.prg",
)
_MIN_CORPUS_DECODED = 100

_LEAD = (("WGh", 0x41), ("AD", 0x09), ("SR", 0xF0), ("PW", 0x08))
_TAIL = (("WGh", 0x40), ("ACID", 0x8123))


def _song() -> DefmonSong:
    """A blank editor song whose sidTAB slots are all active rows."""
    song = DefmonSong()
    for y in range(0x100):
        song.set_jp(y)
    return song


def _set_pattern(song: DefmonSong, p: int, events) -> None:
    events = list(events)
    song.set_pattern_events(p, events + [PatternEvent.delay(0)] * (32 - len(events)))


def _set_row(song: DefmonSong, y: int, columns, dl: int) -> None:
    start = SIDTAB_BANK + y * SIDTAB_ROW_STRIDE - LOAD_ADDRESS
    song.snapshot[start : start + SIDTAB_ROW_STRIDE] = SidtabRow.pack(dict(columns))
    song.set_dl(y, dl)


def _rich_tune() -> Tune:
    return Tune(
        chips=(
            {
                0: Play((1, 2, 3)),
                1: Play((1, 4, 3)),
                2: Jump(0, 2),
                3: Play((1, 1, 1)),
                4: Jump(1),
            },
            {0: Play((5, 5, 5)), 1: Jump(0)},
        ),
        patterns={
            1: (Event(0, a=1, note=0x31), Event(6, a=3, b=4), Event(62, end=0)),
            2: (),
            3: (Event(0, note=0x20), Event(17, note=0x40)),
            4: (Event(0, b=1, end=15),),
            5: (Event(4, a=4, note=0x22, end=3),),
        },
        rows={
            1: Row(_LEAD, 3, 2),
            2: Row(_TAIL),
            3: Row(_TAIL, 1, 3),
            4: Row(),
        },
        counted=True,
        starts=(0, 1),
        tempo=(0x4CC7, 2),
        sid2_base=0xD420,
    )


def _twins() -> Tune:
    return Tune(
        chips=({0: Play((9, 5, 9)), 1: Jump(0, 3), 2: Play((7, 7, 7)), 3: Jump(0)},),
        patterns={
            5: (Event(0, a=20, note=0x30), Event(8, end=1)),
            7: (Event(0, b=30), Event(4, end=0)),
            9: (Event(0, a=10, note=0x30), Event(8, end=1)),
        },
        rows={
            10: Row(_LEAD, 2, 11),
            11: Row(_TAIL, 1, 10),
            20: Row(_LEAD, 2, 21),
            21: Row(_TAIL, 1, 20),
            30: Row(_TAIL),
        },
    )


class TestText(unittest.TestCase):
    """``str(tune)`` and :meth:`Tune.parse` are inverses."""

    def test_round_trip(self):
        tune = _rich_tune()
        self.assertEqual(Tune.parse(str(tune)), tune)

    def test_uncounted_round_trip(self):
        tune = dataclasses.replace(_rich_tune(), counted=False, tempo=None)
        tune = dataclasses.replace(
            tune, chips=({0: Play((1, 1, 1)), 1: Jump(0)},), sid2_base=None
        )
        text = str(tune)
        self.assertIn("jumps uncounted", text)
        self.assertIn("$01: jump $00\n", text)
        self.assertEqual(Tune.parse(text), tune)

    def test_parse_with_comments(self):
        text = """
        # header
        jumps counted
        start $00
        sid 1          # first chip
          $00: $01 $01 $01
          $01: jump $00 x0
        pattern $01
          0: n $31 a $01   # lead
          6:
          62: end 0
        row $01 WGh $41 ACID $8123 hold 3 -> $02
        row $02 stop
        """
        expected = Tune(
            chips=({0: Play((1, 1, 1)), 1: Jump(0)},),
            patterns={1: (Event(0, a=1, note=0x31), Event(6), Event(62, end=0))},
            rows={1: Row((("WGh", 0x41), ("ACID", 0x8123)), 3, 2), 2: Row()},
        )
        self.assertEqual(Tune.parse(text), expected)

    def test_text_lists_acid_as_a_word(self):
        self.assertIn("ACID $8123", str(_rich_tune()))


class TestDecode(unittest.TestCase):
    """:meth:`Tune.from_song` applies the player's read rules."""

    def _base(self) -> DefmonSong:
        song = _song()
        song.set_step(0, v1=1, v2=2, v3=0)
        song.set_step(1, v1=1, v2=0, v3=0)
        song.set_jump(2, target=0)
        song.set_step(5, v1=3, v2=3, v3=3)
        _set_pattern(
            song,
            1,
            [
                PatternEvent.note_on(0x30, slot_a=3, duration=3),
                PatternEvent.delay(5),
                PatternEvent.delay(0),
                PatternEvent.note_on(0x31, slot_b=2, duration=1),
                PatternEvent.alt_end(4),
            ],
        )
        _set_pattern(song, 2, [PatternEvent.note_on(0x40, duration=3)] * 2)
        song.pattern(2)[4] |= PatternEvent.FLAG_ALT
        _set_pattern(
            song, 3, [PatternEvent.note_on(0x50, slot_a=7), PatternEvent.alt_end()]
        )
        _set_row(song, 2, _LEAD, 2)
        song.set_jp(3, 5)
        _set_row(song, 5, _TAIL, 0x80)
        _set_row(song, 7, _LEAD, 0x80)
        return song

    def test_decode(self):
        tune = Tune.from_song(self._base())
        self.assertEqual(
            tune,
            Tune(
                chips=({0: Play((1, 2, 0)), 1: Play((1, 0, 0)), 2: Jump(0)},),
                patterns={
                    0: (),
                    1: (
                        Event(0, a=5, note=0x30),
                        Event(14, b=2, note=0x31),
                        Event(17, end=4),
                    ),
                    2: (Event(0, note=0x40), Event(5, note=0x40, end=3)),
                },
                rows={2: Row(_LEAD, 3, 5), 5: Row(_TAIL)},
            ),
        )

    def test_alt_cuts_every_voice(self):
        song = self._base()
        song.set_jump(1, target=0)
        tune = Tune.from_song(song)
        self.assertEqual(tune.patterns[1], (Event(0, a=5, note=0x30),))
        self.assertEqual(tune.rows, {5: Row(_TAIL)})

    def test_counted_jump_falls_through(self):
        song = self._base()
        song.set_jump(1, target=0, count=2)
        song.set_step(2, v1=1)
        song.set_jump(3, target=0)
        tune = Tune.from_song(song)
        self.assertEqual(
            tune.chips,
            ({0: Play((1, 2, 0)), 1: Jump(0, 2), 2: Play((1, 0, 0)), 3: Jump(0)},),
        )

    def test_uncounted_jump_never_falls_through(self):
        song = self._base()
        song.set_jump(1, target=0, count=2)
        song.layout_entry = Entry("editor", detected())
        song.image_bytes = bytes.fromhex("BE00001000B90000A8BE")
        tune = Tune.from_song(song)
        self.assertFalse(tune.counted)
        self.assertEqual(tune.chips, ({0: Play((1, 2, 0)), 1: Jump(0)},))

    def test_jump_landing_row_is_played(self):
        song = self._base()
        song.set_jump(1, target=4)
        song.set_jump(4, target=0)
        with self.assertRaisesRegex(DefmonError, r"row \$04 plays"):
            Tune.from_song(song)

    def test_jump_landing_row_is_played_as_patterns(self):
        song = self._base()
        song.set_jump(1, target=5)
        song.set_jump(6, target=0)
        tune = Tune.from_song(song)
        self.assertEqual(tune.chips[0][5], Play((3, 3, 3)))
        self.assertEqual(tune.patterns[3], (Event(0, a=7, note=0x50), Event(2, end=0)))
        self.assertEqual(tune.rows[7], Row(_LEAD))

    def test_stereo_tempo(self):
        song = self._base()
        song.set_stereo(True)
        song.set_sid2_base(0xDE00)
        song.set_cia2_timer(0x1234)
        song.set_sub_frame_count(4)
        song.set_step(0, v1=1, v2=2, v3=0, v4=2, v5=2, v6=2)
        song.set_jump(1, target=0, stereo=True)
        tune = Tune.from_song(song)
        self.assertEqual(tune.chips[1], {0: Play((2, 2, 2)), 1: Jump(0)})
        self.assertEqual((tune.tempo, tune.sid2_base), ((0x1234, 4), 0xDE00))

    def test_jp_onto_jp(self):
        song = self._base()
        song.set_jp(5, 6)
        with self.assertRaisesRegex(DefmonError, "JP onto JP"):
            Tune.from_song(song)

    def test_step_that_never_ends(self):
        song = _song()
        with self.assertRaisesRegex(DefmonError, "no pattern that ends"):
            Tune.from_song(song)

    def test_gate_closes_patterns(self):
        song = _song()
        song.snapshot[0x7162 - LOAD_ADDRESS] = 1
        self.assertEqual(Tune.from_song(song).patterns, {0: (Event(62, end=0),)})

    def test_pattern_past_32_steps(self):
        song = self._base()
        song.set_step(0, v1=2)
        _set_pattern(song, 2, [PatternEvent.delay(15)] * 31 + [PatternEvent.alt_end()])
        with self.assertRaisesRegex(DefmonError, "past its 32 steps"):
            Tune.from_song(song)

    def test_pattern_number_out_of_range(self):
        song = self._base()
        song.arranger_v2[1] = 0x80
        with self.assertRaisesRegex(DefmonError, r"row \$01 plays"):
            Tune.from_song(song)


class TestCanonical(unittest.TestCase):
    """:meth:`Tune.canonical` merges what the player cannot tell apart."""

    def test_merges_and_numbers(self):
        self.assertEqual(
            _twins().canonical(),
            Tune(
                chips=(
                    {0: Play((1, 1, 1)), 1: Jump(0, 3), 2: Play((2, 2, 2)), 3: Jump(0)},
                ),
                patterns={
                    1: (Event(0, a=1, note=0x30), Event(8, end=1)),
                    2: (Event(0, b=3), Event(4, end=0)),
                },
                rows={1: Row(_LEAD, 2, 2), 2: Row(_TAIL, 1, 1), 3: Row(_TAIL)},
            ),
        )

    def test_idempotent(self):
        for tune in (_twins(), _rich_tune()):
            canon = tune.canonical()
            self.assertEqual(canon.canonical(), canon)

    def test_uncounted_drops_counts(self):
        tune = dataclasses.replace(_twins(), counted=False)
        self.assertEqual(tune.canonical().chips[0][1], Jump(0))

    def test_identical_rows_merge_into_a_loop(self):
        tune = dataclasses.replace(
            _twins(), rows={**_twins().rows, 21: Row(_LEAD, 2, 20)}
        )
        canon = tune.canonical()
        self.assertEqual(len(canon.patterns), 3)
        loop = canon.patterns[2][0].a
        self.assertEqual(canon.rows[loop], Row(_LEAD, 2, loop))


class TestWrite(unittest.TestCase):
    """:meth:`Tune.to_song` writes a song that decodes to the same tune."""

    def _round(self, tune: Tune) -> None:
        song = tune.to_song()
        self.assertEqual(Tune.from_song(song).canonical(), tune.canonical())
        loaded = DefmonSong.from_bytes(song.to_bytes())
        self.assertEqual(Tune.from_song(loaded).canonical(), tune.canonical())

    def test_round_trip(self):
        self._round(dataclasses.replace(_rich_tune(), starts=(0,)))
        self._round(_twins())

    def test_long_gaps_split(self):
        tune = Tune(
            chips=({0: Play((1, 1, 1)), 1: Jump(0)},),
            patterns={1: (Event(3, note=0x10), Event(43, note=0x11), Event(60, end=2))},
        )
        self._round(tune)
        body = bytes(tune.to_song().pattern(1))
        self.assertEqual(
            [body[i] & 0x0F for i in range(0, 4 * 7, 4)], [1, 15, 15, 4, 15, 2, 0]
        )

    def test_identity_layout(self):
        rows = {2: Row(_LEAD, 3, 5), 5: Row(_TAIL)}
        self.assertEqual(_layout(rows), ({2: 2, 5: 5}, {3: 5}))
        tune = Tune(
            chips=({0: Play((1, 1, 1)), 1: Jump(0)},),
            patterns={1: (Event(0, a=2), Event(4, end=0))},
            rows=rows,
        )
        snap = tune.to_song().snapshot
        self.assertEqual(snap[JP_MARKER - LOAD_ADDRESS + 3], 0)
        self.assertEqual(snap[JP_TARGET - LOAD_ADDRESS + 3], 5)
        self.assertEqual(snap[JP_MARKER - LOAD_ADDRESS + 4], ACTIVE)
        self.assertEqual(Tune.from_song(tune.to_song()), tune)

    def test_fresh_layout(self):
        rows = {2: Row(_LEAD, 3, 5), 3: Row(), 5: Row(_TAIL)}
        self.assertEqual(_layout(rows), ({2: 1, 5: 2, 3: 3}, {}))
        loop = {300: Row(_LEAD, 1, 301), 301: Row(_TAIL, 1, 300)}
        self.assertEqual(_layout(loop), ({300: 1, 301: 2}, {3: 300}))
        tune = Tune(
            chips=({0: Play((1, 1, 1)), 1: Jump(0)},),
            patterns={1: (Event(0, a=2, b=3), Event(4, end=0))},
            rows=rows,
        )
        self._round(tune)

    def test_layout_overflow(self):
        with self.assertRaisesRegex(DefmonError, "sidTAB slots"):
            _layout({0x100 + y: Row() for y in range(0x101)})

    def test_encode_errors(self):
        chips = ({0: Play((1, 1, 1))},)
        for patterns, message in (
            ({1: (Event(0, end=0), Event(4))}, "end event before"),
            ({1: (Event(0), Event(1, end=0))}, "1 tick apart"),
            ({1: tuple(Event(2 * i) for i in range(33))}, "max 32"),
            ({0x80: ()}, "not 0"),
        ):
            with self.assertRaisesRegex(DefmonError, message):
                Tune(chips=chips, patterns=patterns).to_song()


class TestWorktunes(unittest.TestCase):
    """The release worktunes decode, print, write back and sound the same."""

    def _worktune(self, name: str) -> bytes:
        try:
            return fixture_path(name).read_bytes()
        except FileNotFoundError:
            self.skipTest(f"worktune fixture {name} unreachable")
        raise AssertionError(name)

    def _editor(self) -> bytes:
        editor = editor_image_bytes()
        if editor is None:
            self.skipTest("defMON editor image unreachable")
        return editor

    def test_round_trips(self):
        for name in _WORKTUNES:
            with self.subTest(name):
                tune = Tune.from_song(DefmonSong.from_bytes(self._worktune(name)))
                self.assertEqual(Tune.parse(str(tune)), tune)
                canon = tune.canonical()
                written = DefmonSong.from_bytes(canon.to_song().to_bytes())
                self.assertEqual(Tune.from_song(written).canonical(), canon)

    def check_sound(self, name: str) -> None:
        """The editor's player renders the canonical rewrite like the original."""
        editor = self._editor()
        prg = self._worktune(name)
        canon = Tune.from_song(DefmonSong.from_bytes(prg)).canonical()
        original, rewritten = (
            DefmonPlayer(editor_replay(editor, data)).render_grid(_RENDER_FRAMES)
            for data in (prg, canon.to_song().to_bytes())
        )
        self.assertGreater(len(set(map(tuple, original))), 1)
        self.assertEqual(original, rewritten)

    def check_packer(self, name: str) -> None:
        """defMON's packer keeps the canonical tune apart from the tempo."""
        editor = self._editor()
        song = DefmonSong.from_bytes(self._worktune(name))
        tune = dataclasses.replace(Tune.from_song(song).canonical(), tempo=None)
        packed = DefmonSong.from_bytes(song.to_replay(editor))
        self.assertEqual(Tune.from_song(packed).canonical(), tune)


for _name in _SOUND_TUNES:
    setattr(
        TestWorktunes,
        f"test_sound_{_name[:-4]}",
        lambda self, name=_name: self.check_sound(name),
    )
for _name in _PACKER_TUNES:
    setattr(
        TestWorktunes,
        f"test_packer_{_name[:-4]}",
        lambda self, name=_name: self.check_packer(name),
    )


class TestCorpus(unittest.TestCase):
    """HVSC defMON replays decode or are flagged unrepresentable."""

    def test_corpus(self):
        corpus = resolve_corpus()
        if len(corpus) < _MIN_CORPUS_DECODED:
            self.skipTest("HVSC defMON corpus unreachable")
        decoded = 0
        for rel, path in corpus:
            with self.subTest(rel):
                try:
                    tune = Tune.from_song(DefmonSong.from_bytes(path.read_bytes()))
                except DefmonError:
                    continue
                self.assertEqual(Tune.parse(str(tune)), tune)
                decoded += 1
        self.assertGreaterEqual(decoded, _MIN_CORPUS_DECODED)


if __name__ == "__main__":
    unittest.main()
