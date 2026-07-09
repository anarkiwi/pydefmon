"""Corpus test: pydefmon's ``.sid`` reader against real HVSC defMON tunes.

HVSC ships defMON tunes as PSID/RSID ``.sid`` replay files (a relocatable
player + compacted tune data), distinct from the ``.prg`` editor workfile.
This test enumerates every DefMon ``.sid`` in the local HVSC tree exactly as
sidid identifies them (by defMON's player signature) and asserts pydefmon's
public reader recognises and decodes each one.

HVSC tunes are copyright works and are never redistributed with pydefmon.
The test ``skipTest``s cleanly when the tree is absent and runs for real when
it is present -- point it at the tree with ``$HVSC`` (the ``C64Music`` dir),
e.g. ``HVSC=/path/to/hvsc/C64Music``.

A small number of tunes use a newer packer variant with a compact / indirect
data layout that the editor-layout reconstruction does not map; the reader
recognises them (so ``recognize`` / ``detect`` still work) but raises a
specific :class:`DefmonError` rather than fabricating a song. The test
asserts every tune is recognised and that each either decodes to a
structurally sound song or raises that specific error -- never anything else.
"""

import unittest

from pysidtracker import SidImage

from pydefmon.defmon import (
    DefmonError,
    DefmonSong,
    DefmonSidParser,
    STANDARD_SNAPSHOT_SIZE,
)

from tests._support import find_defmon_sids, hvsc_root

# Below this many DefMon tunes we assume a partial HVSC checkout and skip
# rather than assert on an unrepresentative sample.
_MIN_CORPUS = 20
# Marker in the DefmonError message for the unsupported compact/indirect
# packer variant (vs a genuine parse failure).
_VARIANT_MARKER = "compact/indirect packer variant"


def _referenced_patterns(song: DefmonSong):
    seen = set()
    for arr in (song.arranger_v1, song.arranger_v2, song.arranger_v3):
        for b in bytes(arr):
            if b not in (0, 0xFF):
                seen.add(b & 0x7F)
    return sorted(seen)


def _assert_structurally_sound(test, song: DefmonSong, name: str) -> None:
    test.assertEqual(len(song.snapshot), STANDARD_SNAPSHOT_SIZE, name)
    pats = _referenced_patterns(song)
    test.assertTrue(pats, f"{name}: no patterns referenced by any arranger")
    # At least one referenced pattern has a non-empty, decodable body.
    bodies = 0
    for pat in pats:
        events = song.pattern_events(pat)
        test.assertEqual(len(events), 32, f"{name}: pattern {pat} not 32 events")
        if any(bytes(song.pattern(pat))):
            bodies += 1
    test.assertTrue(bodies, f"{name}: all referenced pattern bodies empty")
    # Every sidTAB row decodes without error.
    for y in range(256):
        song.sidtab_row(y)


class TestHvscDefmonSidCorpus(unittest.TestCase):
    """pydefmon's public reader vs every DefMon ``.sid`` in local HVSC."""

    @classmethod
    def setUpClass(cls):
        root = hvsc_root()
        if root is None:
            raise unittest.SkipTest(
                "local HVSC tree not found; set $HVSC to the C64Music dir "
                "to run the DefMon .sid corpus test"
            )
        cls.tunes = find_defmon_sids(root)
        if len(cls.tunes) < _MIN_CORPUS:
            raise unittest.SkipTest(
                f"only {len(cls.tunes)} DefMon .sid found under {root} "
                f"(< {_MIN_CORPUS}); looks like a partial HVSC checkout"
            )

    def test_every_defmon_sid_recognised_and_read(self):
        parser = DefmonSidParser()
        recognised = decoded = variant = 0
        failures = []
        for path in self.tunes:
            name = path.name
            raw = path.read_bytes()
            # recognize() must fire for every sidid-identified DefMon tune.
            image = SidImage.from_bytes(raw)
            if parser.recognize(image) is None:
                failures.append(f"{name}: recognize() returned None")
                continue
            recognised += 1
            try:
                song = parser.parse(raw)
            except DefmonError as e:
                if _VARIANT_MARKER in str(e):
                    variant += 1
                    continue
                failures.append(f"{name}: {e}")
                continue
            except Exception as e:  # pylint: disable=broad-exception-caught
                failures.append(f"{name}: {type(e).__name__}: {e}")
                continue
            try:
                _assert_structurally_sound(self, song, name)
            except AssertionError as e:
                failures.append(str(e))
                continue
            decoded += 1

        total = len(self.tunes)
        if failures:
            head = "\n  ".join(failures[:25])
            self.fail(
                f"{len(failures)}/{total} DefMon .sid failed "
                f"(recognised={recognised} decoded={decoded} variant={variant}):"
                f"\n  {head}"
            )
        self.assertEqual(recognised, total, "every DefMon .sid must be recognised")
        # The overwhelming majority must decode to a sound song; only the
        # known compact/indirect packer variant may be rejected.
        self.assertGreaterEqual(
            decoded,
            total - 10,
            f"too many tunes rejected: decoded={decoded} variant={variant} "
            f"of {total}",
        )

    def test_prg_and_sid_share_one_reader(self):
        """A DefMon ``.sid`` goes through DefmonSong.from_bytes / .read too."""
        for path in self.tunes:
            raw = path.read_bytes()
            try:
                # from_bytes dispatches on PSID/RSID magic to the replay path.
                song = DefmonSong.from_bytes(raw)
            except DefmonError:
                continue
            self.assertEqual(len(song.snapshot), STANDARD_SNAPSHOT_SIZE)
            return
        self.fail("no DefMon .sid decoded via DefmonSong.from_bytes")


if __name__ == "__main__":
    unittest.main()
