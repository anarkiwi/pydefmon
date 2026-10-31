"""The play spec's note-table declarations and the ``SidFormat.spec`` hook.

``fmt.spec_player(model)`` plays the model's own replay as ``DefmonPlayer`` does;
with ``notes=clock`` every read of the pitch table is the standard equal-tempered word of
the note its index means. The declared offsets are held to the replay's own table (on a
tune whose table is standard) and to ``pysidtracker.notes.frequency`` outside it.
"""

import pytest

from pysidtracker import SidImage, song_frames
from pysidtracker.formats import SidFormatError
from pysidtracker.notes import frequency
from pysidtracker.playspec import compile_program, elaborate, parse
from pysidtracker.playspec.pyexec import controls
from pysidtracker.testing import resolve_tune

from pydefmon import DefmonPlayer
from pydefmon.format import defmon_format
from pydefmon.spec import (
    BUILD_FLAGS,
    BUILD_STATE,
    BUILD_TABLES,
    VOICE_STRIDE,
    build_facts,
    pitch_binding,
    player_bases,
    spec,
    spec_entries,
    spec_text,
)

from tests._support import corpus_relpaths

PAL = 985248
STANDARD_TOP = 118
NOTE_BIAS = 24
FMT = defmon_format()
TUNES = (
    "MUSICIANS/D/Demsky_Martin/Acid_Bassline.sid",
    "MUSICIANS/D/Dex-D/Short_Dream.sid",
    "MUSICIANS/I/Ilkke/Klotokok.sid",
)
PLAYED = (
    "MUSICIANS/G/Goto80/Dr_Ghetto.sid",
    "MUSICIANS/D/Demsky_Martin/Minimal.sid",
    "MUSICIANS/D/Demsky_Martin/Wasps.sid",
)
PITCH = parse(spec_text("defmon_pitch"))


def _tune(relpath: str) -> bytes:
    path = resolve_tune(relpath)
    if path is None:
        pytest.skip(f"tune {relpath} unavailable (offline, not cached)")
    return path.read_bytes()


@pytest.mark.parametrize("relpath", corpus_relpaths())
def test_spec_player_plays_the_grid_of_defmon_player(relpath):
    data = _tune(relpath)
    model = FMT.parser.parse(data)
    if len(player_bases(SidImage.from_bytes(data).image)) > 1:
        with pytest.raises(SidFormatError):
            FMT.spec_player(model)
        return
    frames = song_frames(data)
    player = FMT.spec_player(model)
    assert player.render_grid(frames) == DefmonPlayer(data).render_grid(frames)


class Pitch:
    """One voice's pitch step of a replay's post-init memory, native or standard."""

    def __init__(self, data: bytes, standard: "int | None", voice: int = 0):
        self.mem = bytearray(DefmonPlayer(data).memory)
        self.base = player_bases(self.mem)[0]
        self.facts = build_facts(self.mem, self.base)
        self.binding = pitch_binding(self.base, voice)
        self.at = voice * VOICE_STRIDE
        program = elaborate(PITCH, self.binding, standard=standard)
        self.frame = compile_program(program)
        self.controls = controls(program)
        self.inputs = {name[4:]: 0 for name in program.inputs}

    def table(self, name: str, index: int) -> int:
        """Byte ``index`` of the replay's table ``name``."""
        return self.mem[self.facts.tables[name] + index]

    def word(self, index: int) -> int:
        """Entry ``index`` of the replay's own pitch table."""
        return self.table("pitch_hi", index) << 8 | self.table("pitch_lo", index)

    def __call__(self, mode: int, note: int, detune: int = 0, acc: int = 0) -> tuple:
        """``(freq word, acc)`` after one step from these voice cells."""
        cell = {name: addr + self.at for name, addr in self.binding.items()}
        self.mem[cell["mode"]] = mode
        self.mem[cell["note"]] = note
        self.mem[cell["detune"]] = detune
        self.mem[cell["acc_lo"]], self.mem[cell["acc_hi"]] = acc & 0xFF, acc >> 8
        self.frame(
            self.mem.__getitem__,
            self.mem.__setitem__,
            self.controls,
            None,
            self.inputs,
        )
        word = self.mem[cell["freq_hi"]] << 8 | self.mem[cell["freq_lo"]]
        return word, self.mem[cell["acc_hi"]] << 8 | self.mem[cell["acc_lo"]]


def _std(index: int) -> int:
    """The standard word of the note that pitch-table ``index`` means."""
    return frequency(index - NOTE_BIAS, PAL)


@pytest.mark.parametrize("relpath", TUNES)
def test_declared_offsets_agree_with_a_standard_native_table(relpath):
    data = _tune(relpath)
    native, standard = Pitch(data, None), Pitch(data, PAL)
    assert all(native.word(i) == _std(i) for i in range(STANDARD_TOP + 1))
    checked = 0
    for note in range(STANDARD_TOP + 1):
        assert native(0, note, 0x21) == standard(0, note, 0x21)
        for mode in range(1, 0x80):
            y = (mode + note) & 0xFF
            if 25 <= y <= STANDARD_TOP + 24:
                assert native(mode, note) == standard(mode, note), (mode, note)
                checked += 1
    assert checked > 1000


@pytest.mark.parametrize("relpath", TUNES)
def test_standard_notes_are_the_equal_tempered_words_of_the_note_indexes(relpath):
    standard = Pitch(_tune(relpath), PAL)
    for note in range(256):
        word = _std(note)
        for detune in (0, 0x37):
            expect = word & 0xFF00 | (word + detune) & 0xFF
            assert standard(0, note, detune)[0] == expect, note
        for mode in range(1, 0x80):
            y = (mode + note) & 0xFF
            step = _std(y - NOTE_BIAS) - _std(y - NOTE_BIAS - 1)
            assert standard(mode, note)[0] == (word + step) & 0xFFFF, (mode, note)


@pytest.mark.parametrize("relpath", TUNES)
def test_standard_notes_under_a_slide_add_the_accumulator(relpath):
    standard = Pitch(_tune(relpath), PAL)
    for mode in (0x80, 0x81, 0xBF, 0xC0, 0xC1, 0xFF):
        r = mode << 1 & 0x7F
        rate = standard.table("rate_hi", r) << 8 | standard.table("rate_lo", r)
        for note in (0, 23, 24, 60, 118, 119, 255):
            word, acc = standard(mode, note, acc=0x1234)
            assert acc == (0x1234 + (rate if mode < 0xC0 else -rate)) & 0xFFFF
            assert word == (_std(note) + acc) & 0xFFFF


def _voices(player):
    """``(cells, facts, voice offset)`` of every voice of every copy of the player."""
    mem = player.memory
    for base in player_bases(mem):
        facts = build_facts(mem, base)
        for voice in range(3):
            at = voice * VOICE_STRIDE
            yield {name: mem[addr + at] for name, addr in facts.state.items()}


def test_whole_spec_with_notes_plays_the_standard_word_of_every_note():
    seen = set()
    for relpath in PLAYED:
        data = _tune(relpath)
        player = FMT.spec_player(FMT.parser.parse(data), notes=PAL)
        for _ in range(600):
            player.play_frame()
            for cells in _voices(player):
                note, mode = cells["note"], cells["mode"]
                word = cells["freq_hi"] << 8 | cells["freq_lo"]
                seen.add("negative" * (0 < note < NOTE_BIAS))
                seen.add("past top" * (note > STANDARD_TOP))
                if mode == 0:
                    low = (_std(note) + cells["detune"]) & 0xFF
                    assert word == _std(note) & 0xFF00 | low
                elif mode < 0x80:
                    y = (mode + note) & 0xFF
                    step = _std(y - NOTE_BIAS) - _std(y - NOTE_BIAS - 1)
                    assert word == (_std(note) + step) & 0xFFFF
                    seen.add("portamento")
                    seen.add("alias below table" * (y < NOTE_BIAS + 1))
    assert {"negative", "past top", "portamento", "alias below table"} <= seen


def test_build_facts_reports_the_binding_of_the_player():
    mem = DefmonPlayer(_tune(TUNES[0])).memory
    base = player_bases(mem)[0]
    facts = build_facts(mem, base)
    assert tuple(facts.flags) == BUILD_FLAGS
    assert tuple(facts.tables) == BUILD_TABLES
    assert tuple(facts.state) == BUILD_STATE
    assert facts.base == base
    assert facts.binding["pitch_lo"] == facts.tables["pitch_lo"]
    assert facts.flags["reduced"] is False
    assert build_facts(mem, base, base + 6).flags["reduced"] == (
        spec_entries(mem) >= {base + 6}
    )
    assert spec() is spec()


NTSC = 1022727
CLOCK_BITS = 0x76 + 1
NTSC_CLOCK = 0b1000


def test_notes_follow_the_clock_of_the_tunes_header():
    data = bytearray(_tune(PLAYED[0]))
    data[CLOCK_BITS] = data[CLOCK_BITS] & ~0b1100 | NTSC_CLOCK
    assert SidImage.from_bytes(bytes(data)).header.is_ntsc
    player = FMT.spec_player(FMT.parser.parse(bytes(data)), notes=True)
    pal = FMT.spec_player(FMT.parser.parse(_tune(PLAYED[0])), notes=True)
    sounded = 0
    for _ in range(300):
        player.play_frame()
        pal.play_frame()
        for cells in _voices(player):
            if cells["mode"] == 0 and cells["note"]:
                word = cells["freq_hi"] << 8 | cells["freq_lo"]
                want = frequency(cells["note"] - NOTE_BIAS, NTSC)
                assert word == want & 0xFF00 | (want + cells["detune"]) & 0xFF
                sounded += 1
    assert sounded
    assert any(a != b for a, b in zip(_voices(player), _voices(pal)))
