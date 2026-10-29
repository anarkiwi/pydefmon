"""The player behaviour ``docs/format.md`` documents, checked on the editor's own player."""

import itertools
import unittest

from pydefmon import NOTE_PITCH_HI, NOTE_PITCH_LO, DefmonSong, Tune

from tests._support import editor_grid

_FREQ = (0, 7, 14)  # freq lo per voice; hi is the next register
_CTRL = (4, 11, 18)
_PW_LO, _PW_HI, _MODE_VOL = 2, 3, 24
_N = 0x31
_SILENT = "pattern $00\n"


def _pitch(note: int) -> int:
    return NOTE_PITCH_HI[note & 0xFF] << 8 | NOTE_PITCH_LO[note & 0xFF]


def _freq(frame, voice: int = 0) -> int:
    return frame[_FREQ[voice]] | frame[_FREQ[voice] + 1] << 8


def _runs(values) -> list[tuple[int, int]]:
    return [(k, len(list(g))) for k, g in itertools.groupby(values)]


def _one_voice(rows: str, events: str = "", frames: int = 24):
    return editor_grid(
        f"sid 1\n $00: $01 $00 $00\n{_SILENT}pattern $01\n"
        f" 0: n ${_N:02x} a $01\n{events} 200: end 0\n{rows}",
        frames,
    )


class TestPatternTiming(unittest.TestCase):
    def test_step_lasts_duration_plus_two_ticks(self):
        text = (
            "sid 1\n $00: $01 $00 $00\n $01: jump $01 x0\n"
            f"{_SILENT}pattern $01\n 0: n $31\n 5: n $32\n 7: n $33\n 30: end 0\n"
        )
        steps = Tune.parse(text).to_song().pattern_events(1)
        self.assertEqual([ev.duration for ev in steps[:3]], [3, 0, 15])
        runs = _runs(_freq(f) for f in editor_grid(text, 12))
        self.assertEqual([n for _, n in runs[2:4]], [3 + 2, 0 + 2])

    def test_alt_on_one_voice_ends_the_step_for_all(self):
        grid = editor_grid(
            "sid 1\n $00: $01 $02 $00\n $01: $03 $03 $00\n $02: jump $02 x0\n"
            f"{_SILENT}pattern $01\n 0: n $31\n 4: end 2\n"
            "pattern $02\n 0: n $31\n 6: n $40\n 60: end 0\n"
            "pattern $03\n 0: n $50\n 60: end 0\n",
            16,
        )
        v1, v2 = ([_freq(f, v) for f in grid] for v in (0, 1))
        start = v1.index(_pitch(0x31))
        self.assertNotIn(_pitch(0x40) + 1, v2)
        self.assertEqual(v1.index(_pitch(0x50)) - start, 4 + 2 + 2)
        self.assertEqual(v2.index(_pitch(0x50) + 1) - start, 4 + 2 + 2)


class TestCascade(unittest.TestCase):
    def test_slot_fetches_in_the_frame_its_event_applies(self):
        grid = _one_voice("row $01 WGh $20 WGl $01 stop\n")
        freq = [_freq(f) for f in grid]
        ctrl = [f[_CTRL[0]] for f in grid]
        self.assertEqual(freq.index(_pitch(_N)), ctrl.index(0x21))

    def test_layer_b_applies_after_layer_a(self):
        grid = editor_grid(
            f"sid 1\n $00: $01 $00 $00\n{_SILENT}pattern $01\n"
            " 0: n $31 a $01 b $02\n 200: end 0\n"
            "row $01 WGh $10 WGl $01 stop\nrow $02 WGh $80 stop\n",
            6,
        )
        self.assertEqual(grid[-1][_CTRL[0]], 0x81)

    def test_dl_holds_dl_plus_one_frames_and_stop_freezes(self):
        grid = _one_voice(
            "row $01 WGh $20 WGl $01 hold 3 -> $02\nrow $02 WGl $00 hold 5 -> $03\n"
            "row $03 WGl $01 stop\n"
        )
        runs = _runs(f[_CTRL[0]] for f in grid)
        self.assertEqual(runs[1:3], [(0x21, 3), (0x20, 5)])
        self.assertEqual(runs[3][0], 0x21)
        self.assertEqual(runs[-1], runs[3])


class TestColumns(unittest.TestCase):
    def test_note_frequency_and_per_voice_detune(self):
        grid = editor_grid(
            "sid 1\n $00: $01 $01 $01\npattern $01\n 0: n $31\n 200: end 0\n", 6
        )
        self.assertEqual(
            [_freq(grid[-1], v) for v in range(3)],
            [_pitch(_N), _pitch(_N) + 1, _pitch(_N) + 2],
        )

    def test_low_af_is_a_constant_detune_not_a_portamento(self):
        for af in (0x01, 0x05, 0x0C, 0x18):
            with self.subTest(af=af):
                grid = editor_grid(
                    "sid 1\n $00: $01 $01 $00\n"
                    f"{_SILENT}pattern $01\n 0: n $31 a $01\n 200: end 0\n"
                    f"row $01 AF ${af:02x} stop\n",
                    12,
                )
                step = _pitch(_N + af - 24) - _pitch(_N + af - 25)
                for voice in (0, 1):
                    self.assertEqual(
                        {_freq(f, voice) for f in grid[3:]}, {_pitch(_N) + step}
                    )

    def test_high_af_slides_each_frame_until_a_note(self):
        for af, sign in ((0x81, 1), (0xC1, -1)):
            with self.subTest(af=af):
                grid = _one_voice(f"row $01 AF ${af:02x} stop\n", " 8: n $31\n", 16)
                freq = [_freq(f) for f in grid]
                start = freq.index(_pitch(_N) + sign)
                self.assertEqual(
                    freq[start : start + 4],
                    [_pitch(_N) + sign * k for k in (1, 2, 3, 4)],
                )
                self.assertEqual(freq[-1], _pitch(_N))

    def test_pw_low_nibble_is_the_high_register(self):
        frame = _one_voice("row $01 PW $3a stop\n", frames=6)[-1]
        self.assertEqual((frame[_PW_HI] & 0x0F, frame[_PW_LO]), (0x0A, 0x30))

    def test_ps_adds_depth_and_7f_or_subtracts_depth_plus_one(self):
        for depth, step in ((0x84, 0x04), (0xC0, 0x40), (0x04, -0x05), (0x40, -0x41)):
            with self.subTest(depth=depth):
                grid = _one_voice(f"row $01 PW $88 PS ${depth:02x} stop\n", frames=10)
                pulse = [(f[_PW_HI] & 0x0F) << 8 | f[_PW_LO] for f in grid[3:]]
                self.assertEqual({b - a for a, b in zip(pulse, pulse[1:])}, {step})

    def test_fv_sets_mode_and_volume_stays_full(self):
        frame = _one_voice("row $01 FV $50 stop\n", frames=6)[-1]
        self.assertEqual(frame[_MODE_VOL], 0x5F)

    def test_tr_absolute_with_bit7_else_relative_to_the_note(self):
        grid = _one_voice(
            "row $01 TR $02 hold 4 -> $02\nrow $02 TR $85 hold 4 -> $03\n"
            "row $03 TR $7f hold 4 -> $04\nrow $04 TR $00 stop\n",
            frames=20,
        )
        notes = [k for k, _ in _runs(_freq(f) for f in grid)][2:]
        self.assertEqual(
            notes,
            [_pitch(_N + 2), _pitch(0x05), _pitch((_N + 0x7F) & 0x7F), _pitch(_N)],
        )


class TestArranger(unittest.TestCase):
    _PATTERNS = "".join(
        f"pattern ${p:02x}\n 0: n ${0x30 + p:02x}\n 2: end 0\n" for p in (1, 2, 3)
    )

    def _notes(self, rows: str, frames: int) -> list[tuple[int, int]]:
        grid = editor_grid(f"sid 1\n{rows}{_SILENT}{self._PATTERNS}", frames)
        return _runs(_freq(f) for f in grid)[2:]

    def test_counted_jump_plays_its_body_count_plus_one_times(self):
        runs = self._notes(
            " $00: $01 $00 $00\n $01: jump $00 x2\n $02: $02 $00 $00\n"
            " $03: $03 $00 $00\n $04: jump $03 x0\n",
            30,
        )
        self.assertEqual(runs[:2], [(_pitch(0x31), 3 * 4), (_pitch(0x32), 4)])

    def test_jump_onto_a_jump_row_does_not_chain(self):
        runs = self._notes(
            " $00: $01 $00 $00\n $01: jump $03 x0\n $02: $02 $00 $00\n"
            " $03: jump $02 x0\n",
            30,
        )
        self.assertEqual(runs[0][0], _pitch(0x31))
        self.assertNotIn(_pitch(0x32), [k for k, _ in runs])


class TestSidcallFrames(unittest.TestCase):
    """``DefmonSong.sidcall_frames`` walks rows as the player fetches them."""

    def _song(self) -> DefmonSong:
        song = DefmonSong()
        song.snapshot[0x715E - 0x1800] = 1
        for y in range(256):
            song.set_jp(y)
        return song

    def test_row_255_wraps_to_0(self):
        song = self._song()
        song.set_dl(0, 0x80)
        self.assertEqual([f.row_index for f in song.sidcall_frames(255)], [255, 0])

    def test_jp_redirects_one_level_only(self):
        song = self._song()
        song.set_jp(5, target=9)
        song.set_jp(9, target=2)
        song.set_jp(1, target=5)
        self.assertEqual([f.row_index for f in song.sidcall_frames(0)], [0])
        song.set_jp(9)
        song.set_dl(9, 0x80)
        self.assertEqual([f.row_index for f in song.sidcall_frames(4)], [4, 9])


if __name__ == "__main__":
    unittest.main()
