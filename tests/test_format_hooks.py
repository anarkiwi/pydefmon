"""defMON's ``SidFormat`` hooks: grammar listing, canonical text and snapshot probes."""

import unittest

from pydefmon import DefmonError, Tune
from pydefmon.format import defmon_format

from tests.test_tune import _song


class TestFormatHooks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.song = Tune.parse(
            "sid 1\n $00: $01 $01 $01\n $01: jump $00 x0\n"
            "pattern $01\n 0: n $31 a $02\n 4: end 0\n"
            "row $02 WGh $20 WGl $01 hold 2 -> $03\nrow $03 WGl $00 stop\n"
        ).to_song()
        cls.fmt = defmon_format()

    def test_listing_shows_the_streams_the_player_reads(self):
        text = "\n".join(self.fmt.listing(self.song))
        self.assertEqual(
            [line for line in text.splitlines() if line.endswith(":")],
            ["arranger sid1:", "pattern $01:", "row $02:", "row $03:"],
        )
        self.assertIn("column T_WGH BYTE=$20", text)

    def test_canonical_text_is_the_canonical_tune(self):
        self.assertEqual(
            self.fmt.canonical_text(self.song),
            str(Tune.from_song(self.song).canonical()),
        )

    def test_snapshot_probes_include_the_ram_image_and_canonical_text(self):
        probes = self.fmt.snapshot_probes()
        self.assertEqual(probes["snapshot"](self.song), bytes(self.song.snapshot))
        self.assertIn(b"pattern $01", probes["canonical"](self.song))

    def test_a_song_the_player_cannot_read_raises(self):
        with self.assertRaises(DefmonError):
            list(self.fmt.listing(_song()))


if __name__ == "__main__":
    unittest.main()
