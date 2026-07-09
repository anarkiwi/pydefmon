"""Frame-exact oracle verification for the Goto80 compact-runtime decoder.

The four ``MUSICIANS/G/Goto80`` DefMon replays carry the defMON player's
SID-write signature but store their song data in an older, more compact
runtime encoding (variable-length control-byte pattern streams + relocated
sidTAB bitmask bodies) that :mod:`pydefmon._goto80_format` re-expands into the
editor-layout runtime image :class:`~pydefmon.defmon.DefmonSong` /
:class:`~pydefmon.defmon_player.DefmonPlayer` consume.

This test proves the *decode is correct* by running the reconstructed song
through ``DefmonPlayer`` and comparing its per-frame SID-register output to a
clean-room py65 oracle running the real replay (:mod:`tests._support.
py65_oracle`), over a bounded window. It asserts the cascade-driven registers
-- waveform (CTRL) + envelope (AD/SR) for all three voices -- match the oracle
FRAME-EXACT, which validates the recovered sidTAB programs, pattern
GATE_A/GATE_B/GATE_N gating, the arranger, the DL/duration column and the JP
handling end to end. It also pins the exact set of registers that still
diverge to the known DefmonPlayer model gaps (the TR-note ``&$7F`` mask, the
pulse-width-sweep SBC carry approximation, and the runtime SID-model cutoff
detect), so a decoder regression -- or divergence spreading beyond those
registers -- fails the test. It does NOT claim full byte-exactness.

Tunes are fetched from the HVSC mirror (or the local ``$HVSC`` tree) and
cached; they are never committed. Requires the ``emu`` extra for py65.
"""

import unittest

from pydefmon.defmon import DefmonSong
from pydefmon.defmon_player import DefmonPlayer
from pydefmon._goto80_format import seed_player_from_replay

from tests._support import resolve_tune

_FRAMES = 60

# Voice waveform (CTRL) + envelope (AD/SR) register indices into the 25-wide
# $D400 grid, for all three voices. These are driven purely by the recovered
# sidTAB cascade + pattern gating, so frame-exactness here verifies the decode.
_CASCADE_REGS = (4, 5, 6, 11, 12, 13, 18, 19, 20)

# Registers that may still diverge -- all attributable to documented
# DefmonPlayer model gaps, NOT the decoder:
#   0,1  FREQ1 -- current_note computed via DefmonPlayer's TR handler, which
#         masks (tr+transpose) to 7 bits + treats bit7 as absolute; real
#         defMON ($16ED) does an unmasked 8-bit add, so out-of-range notes
#         (which index an extended note table) differ.
#   9,16,17 PW2LO / PW3LO / PW3HI -- the pulse-width sweep's SUB-path SBC
#         inherits an uncontrolled carry from the pitch oscillator that
#         DefmonPlayer approximates as 0 (see _ps_voice docstring).
#   22   D416 filter cutoff -- defMON's runtime 6581/8580 detect patches the
#         cutoff floor + output scaling; DefmonPlayer hardcodes one branch.
_ALLOWED_DIVERGENT = {0, 1, 9, 16, 17, 22}

_TUNES = (
    "MUSICIANS/G/Goto80/Evil_Wizard_2.sid",
    "MUSICIANS/G/Goto80/Rent-A-Cop.sid",
    "MUSICIANS/G/Goto80/Rent-A-Cop_Reloaded.sid",
    "MUSICIANS/G/Goto80/Rent-A-Cop_Reloaded_title.sid",
)


def _player_grid(raw: bytes, frames: int):
    song = DefmonSong.from_bytes(raw)
    player = DefmonPlayer(song)
    # defMON's init leaves the per-voice PW/FREQ/slide work-block operands at
    # their loaded (author) values; seed them so frame 0 (output-then-compute)
    # matches the oracle.
    seed_player_from_replay(player, raw)
    regs = [0] * 25
    grid = []
    for _ in range(frames):
        for reg, val in player.play_frame():
            idx = reg - 0xD400
            if 0 <= idx < 25:
                regs[idx] = val
        grid.append(list(regs))
    return grid


class TestGoto80OracleFrameExact(unittest.TestCase):
    """Reconstructed Goto80 songs vs the py65 register oracle."""

    @classmethod
    def setUpClass(cls):
        try:
            import tests._support.py65_oracle  # noqa: F401
        except ImportError as exc:
            raise unittest.SkipTest(f"py65 not available ({exc}); pip install .[emu]")
        cls.resolved = [(rel, resolve_tune(rel)) for rel in _TUNES]
        if not any(path is not None for _rel, path in cls.resolved):
            raise unittest.SkipTest(
                "no Goto80 tune resolvable (offline and no local $HVSC tree)"
            )

    def test_cascade_frame_exact_and_divergence_bounded(self):
        from tests._support.py65_oracle import Oracle

        checked = 0
        for rel, path in self.resolved:
            if path is None:
                continue
            with self.subTest(tune=rel):
                raw = path.read_bytes()
                oracle = Oracle(raw).grid(_FRAMES)
                player = _player_grid(raw, _FRAMES)
                n = min(len(oracle), len(player), _FRAMES)
                self.assertGreater(n, 0, f"{rel}: no frames produced")

                # Cascade (waveform + envelope) must be frame-exact.
                for f in range(n):
                    for r in _CASCADE_REGS:
                        self.assertEqual(
                            oracle[f][r],
                            player[f][r],
                            f"{rel}: cascade reg {r} differs at frame {f} "
                            f"(oracle=${oracle[f][r]:02X} player=${player[f][r]:02X})",
                        )

                # Any remaining divergence must fall only in the documented
                # DefmonPlayer model-gap registers.
                divergent = {
                    r
                    for f in range(n)
                    for r in range(25)
                    if oracle[f][r] != player[f][r]
                }
                unexpected = divergent - _ALLOWED_DIVERGENT
                self.assertEqual(
                    unexpected,
                    set(),
                    f"{rel}: unexpected register divergence {sorted(unexpected)} "
                    "(decoder regression?)",
                )
                checked += 1

        self.assertGreater(checked, 0, "no Goto80 tune was verified")


if __name__ == "__main__":
    unittest.main()
