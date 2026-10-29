"""sidTAB WGh / WGl on the editor's own player: ``$D404 = WGh XOR WGl``.

WGh carries the waveform nibble and WGl the gate/sync/ring/test nibble, each
written only by a row that has that column, so a row can change the gate and
keep the waveform (or the reverse). Nothing toggles either byte between fetches.
"""

import itertools
import unittest

from pydefmon import DefmonPlayer, Tune
from pydefmon._worktune_oracle import editor_replay

from tests._support import editor_image_bytes

_CTRL = 4  # voice 1 control register, $D404
_HOLD = 6
_HEAD = """
sid 1
  $00: $01 $00 $00
  $01: jump $00 x0
pattern $00
pattern $01
  0: n $31 a $01
  100: end 0
"""


def _ctrl_runs(rows: str, frames: int = 48) -> list[tuple[int, int]]:
    """(value, frames) runs of voice 1's ``$D404`` after the note; the held last run is None."""
    editor = editor_image_bytes()
    if editor is None:
        raise unittest.SkipTest("defMON editor image unavailable")
    prg = Tune.parse(_HEAD + rows).to_song().to_bytes()
    grid = DefmonPlayer(editor_replay(editor, prg)).render_grid(frames)
    runs = [(k, len(list(g))) for k, g in itertools.groupby(f[_CTRL] for f in grid)]
    return runs[1:-1] + [(runs[-1][0], None)]


class TestWgColumns(unittest.TestCase):
    def test_wgl_gates_without_touching_waveform(self):
        runs = _ctrl_runs(f"""
            row $01 WGh $20 WGl $01 hold {_HOLD} -> $02
            row $02 WGl $00 hold {_HOLD} -> $03
            row $03 WGh $40 hold {_HOLD} -> $04
            row $04 WGl $01 stop
            """)
        self.assertEqual(
            runs, [(0x21, _HOLD), (0x20, _HOLD), (0x40, _HOLD), (0x41, None)]
        )

    def test_absent_columns_keep_their_last_write(self):
        runs = _ctrl_runs(f"""
            row $01 WGh $80 WGl $01 hold {_HOLD} -> $02
            row $02 AD $09 hold {_HOLD} -> $03
            row $03 WGh -- WGl -- SR $f0 stop
            """)
        self.assertEqual(runs, [(0x81, None)])

    def test_bytes_combine_by_xor(self):
        runs = _ctrl_runs(f"""
            row $01 WGh $41 WGl $00 hold {_HOLD} -> $02
            row $02 WGl $01 hold {_HOLD} -> $03
            row $03 WGl $05 stop
            """)
        self.assertEqual(runs, [(0x41, _HOLD), (0x40, _HOLD), (0x44, None)])


if __name__ == "__main__":
    unittest.main()
