"""pydefmon's .prg worktune LOAD model checked against defMON itself (issues #33, #40).

Runs defMON's ``$CEB2`` post-LOAD chain on the real decrunched editor as the
oracle: ``unpacked_snapshot()`` must reproduce its post-LOAD image over the
codec's write range, for the worktunes' own gates and for altered ones.
"""

import unittest

from pysidtracker.emu import EmulatorUnavailable

from pydefmon import DefmonSong
from pydefmon.defmon import GATE_PAT_BASE, GATE_PATTERN_MARKER, GATE_SONG_POSITION
from pydefmon._worktune_oracle import load_worktune
from tests._support import editor_image_bytes, fixture_path

# (xform $715E, pat-base $715F, marker $7162); (1, 1, 1) is covered by test_unpacked_matches_defmon_load.
_GATES = (
    (0, 0, 0),
    (1, 0, 0),
    (0, 1, 0),
    (0, 0, 1),
    (2, 2, 2),  # non-one gate values: defMON's beq treats any non-zero as set
)


def _with_gates(raw: bytes, gates: tuple[int, int, int]) -> bytes:
    """Re-save ``raw`` (defMON's own ``$D626`` walk) with the given gates."""
    song = DefmonSong.from_bytes(raw)
    song.snapshot[GATE_SONG_POSITION] = gates[0]
    song.snapshot[GATE_PAT_BASE] = gates[1]
    song.snapshot[GATE_PATTERN_MARKER] = gates[2]
    return song.to_bytes()


_WORKTUNES = (
    "automatas2017.prg",
    "dansa_in.prg",
    "f7sus4_ngc1277.prg",
    "glow_worm.prg",
    "hive_machines.prg",
    "matsam0t.prg",
    "mondaynight2017.prg",
    "radio_robon95.prg",
    "vacuole.prg",
)


def _dest_top(body: bytes) -> int:
    dest_lo = body[-2]
    dest_hi = body[-1] + 0x18
    return dest_lo | (dest_hi << 8)


class TestNativeWorktunes(unittest.TestCase):
    """The nine shipped worktunes, decoded by defMON itself as the oracle."""

    @classmethod
    def setUpClass(cls):
        cls.editor = editor_image_bytes()

    def setUp(self):
        if self.editor is None:
            self.skipTest("defMON editor image unavailable (offline runner)")

    def _worktunes(self):
        names = []
        for n in _WORKTUNES:
            try:
                fixture_path(n)
                names.append(n)
            except FileNotFoundError:
                pass
        if not names:
            self.skipTest("no worktune fixtures available (offline runner)")
        return names

    def test_unpacked_matches_defmon_load(self):
        """``unpacked_snapshot()`` equals defMON's own ``$CEB2`` LOAD result.

        Compared over ``$1800..``top``: two of the nine tunes encode a shorter
        run (``top``=$7163), and the bytes at/above it retain the editor's prior
        RAM rather than authored data, so they are outside the codec's write range.
        """
        for name in self._worktunes():
            raw = fixture_path(name).read_bytes()
            try:
                ref = load_worktune(self.editor, raw)
            except EmulatorUnavailable:
                self.skipTest("6502 backend unavailable")
            got = bytes(DefmonSong.from_bytes(raw).unpacked_snapshot())
            n = _dest_top(raw[2:]) - 0x1800 + 1
            self.assertEqual(
                got[:n],
                ref[:n],
                f"{name}: unpacked_snapshot differs from defMON's own LOAD over "
                f"$1800..${_dest_top(raw[2:]) - 1:04X}",
            )

    def test_round_trip_preserves_post_load_model(self):
        """``to_bytes`` -> ``from_bytes`` -> ``unpacked_snapshot`` is stable."""
        for name in self._worktunes():
            raw = fixture_path(name).read_bytes()
            song = DefmonSong.from_bytes(raw)
            before = bytes(song.unpacked_snapshot())
            after = bytes(DefmonSong.from_bytes(song.to_bytes()).unpacked_snapshot())
            self.assertEqual(
                before, after, f"{name}: round trip changed the post-LOAD model"
            )

    def test_gated_passes_match_defmon_load(self):
        """``post_load_chain`` runs each pass iff its gate byte is non-zero (issue #33).

        Re-save each worktune with altered ``$715E``/``$715F``/``$7162`` gates
        and compare ``unpacked_snapshot()`` over the codec's write range
        against defMON's own ``$CEB2`` LOAD of that file.
        """
        for name in self._worktunes():
            raw = fixture_path(name).read_bytes()
            for gates in _GATES:
                prg = _with_gates(raw, gates)
                try:
                    ref = load_worktune(self.editor, prg)
                except EmulatorUnavailable:
                    self.skipTest("6502 backend unavailable")
                got = bytes(DefmonSong.from_bytes(prg).unpacked_snapshot())
                n = _dest_top(prg[2:]) - 0x1800 + 1
                self.assertEqual(
                    got[:n],
                    ref[:n],
                    f"{name} gates={gates}: gated post-LOAD passes differ from "
                    f"defMON's own LOAD over $1800..${_dest_top(prg[2:]) - 1:04X}",
                )

    def test_gates_persist_through_to_bytes(self):
        """``to_bytes`` keeps whatever gates the source carried (issue #33)."""
        for name in self._worktunes():
            raw = fixture_path(name).read_bytes()
            for gates in _GATES:
                song = DefmonSong.from_bytes(raw)
                song.snapshot[GATE_SONG_POSITION] = gates[0]
                song.snapshot[GATE_PAT_BASE] = gates[1]
                song.snapshot[GATE_PATTERN_MARKER] = gates[2]
                reloaded = DefmonSong.from_bytes(song.to_bytes()).snapshot
                self.assertEqual(
                    (
                        reloaded[GATE_SONG_POSITION],
                        reloaded[GATE_PAT_BASE],
                        reloaded[GATE_PATTERN_MARKER],
                    ),
                    gates,
                    f"{name}: gates {gates} lost across to_bytes/from_bytes",
                )

    def test_gate_semantics_offline(self):
        """Gate byte semantics without the editor oracle (issue #33).

        All gates zero selects no pass (V20140612-era saves leave
        ``$1A00..$1AFF`` and the ``$11`` markers as decoded); any non-zero
        gate value still selects its pass, and every pass clears its gate.
        """
        for name in self._worktunes():
            raw = fixture_path(name).read_bytes()
            song = DefmonSong.from_bytes(raw)
            for gates in ((0, 0, 0), (7, 0, 9)):
                song.snapshot[GATE_SONG_POSITION] = gates[0]
                song.snapshot[GATE_PAT_BASE] = gates[1]
                song.snapshot[GATE_PATTERN_MARKER] = gates[2]
                decoded = bytes(song.snapshot)
                out = bytes(song.unpacked_snapshot())
                self.assertEqual(
                    (
                        out[GATE_SONG_POSITION],
                        out[GATE_PAT_BASE],
                        out[GATE_PATTERN_MARKER],
                    ),
                    (0, 0, 0),
                    f"{name} gates={gates}: post-LOAD gates not cleared",
                )
                if gates == (0, 0, 0):
                    self.assertEqual(
                        out, decoded, f"{name}: zero gates must apply no pass"
                    )


if __name__ == "__main__":
    unittest.main()
