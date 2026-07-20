"""Unit tests for the defMON PSID/RSID replay reader (``_sid_format``).

These build a synthetic in-memory replay (no copyrighted HVSC material) so
the ``.sid`` reader path is exercised without the corpus fixtures. The
:mod:`tests.test_hvsc_sid_corpus` module validates against real HVSC tunes
when the local tree is present.
"""

import struct
import unittest

from pysidtracker import SidImage

from pydefmon._sid_format import SIGNATURE, depack_replay, find_signature
from pydefmon.defmon import DefmonError, DefmonSong, DefmonSidParser

LOAD = 0x1000
SIG_ADDR = 0x1022  # canonical play body -> data base 0x1800
DATA_BASE = 0x1800
ROW_ADDR = 0x2000  # compacted sidTAB row 0 body
PAT_ADDR = 0x2010  # compacted pattern 1 body

# Variable-length bodies: flag byte + one payload byte per set gate bit.
_PAT1_PACKED = bytes([0x10, 0x41, 0x62, 0x05, 0x06, 0x03, 0x84])
_PAT2_PACKED = bytes([0x51, 0x07, 0x42, 0x80])
PAT2_ADDR = PAT_ADDR + len(_PAT1_PACKED)  # abuts pattern 1: overrun detector

_PAT1_EVENTS = (
    (0x10, 0, 0, 0x41),
    (0x62, 0x05, 0x06, 0),
    (0x03, 0, 0, 0),
    (0x84, 0, 0, 0),
)
_PAT2_EVENTS = ((0x51, 0x07, 0, 0x42), (0x80, 0, 0, 0))


def _psid_header(data_offset: int) -> bytearray:
    h = bytearray(data_offset)
    h[0:4] = b"PSID"
    struct.pack_into(">H", h, 0x04, 2)  # version
    struct.pack_into(">H", h, 0x06, data_offset)
    struct.pack_into(">H", h, 0x08, LOAD)  # loadAddress
    struct.pack_into(">H", h, 0x0A, LOAD)  # initAddress
    struct.pack_into(">H", h, 0x0C, 0x1003)  # playAddress
    struct.pack_into(">H", h, 0x0E, 1)  # songs
    struct.pack_into(">H", h, 0x10, 1)  # startSong
    return h


def _put(image: bytearray, addr: int, data: bytes) -> None:
    off = addr - LOAD
    image[off : off + len(data)] = data


def _synthetic_replay(*, with_pattern: bool = True) -> bytes:
    """A minimal but structurally valid defMON PSID replay."""
    image = bytearray(0x1100)  # covers $1000..$2100

    # Player signature at the canonical play body (wildcards -> 0).
    sig = bytes(0 if b is None else b for b in SIGNATURE)
    _put(image, SIG_ADDR, sig)

    # sidTAB row-0 pointer pair ($1800/$1900): active (hi != 0), body @ ROW_ADDR.
    _put(image, DATA_BASE + 0x000, bytes([ROW_ADDR & 0xFF]))  # lo
    _put(image, DATA_BASE + 0x100, bytes([ROW_ADDR >> 8]))  # hi ($19,0 = active)
    # sidTAB row 1 as a JP source back to row 0 (hi == 0).
    _put(image, DATA_BASE + 0x001, bytes([0x00]))  # jp target row 0
    _put(image, DATA_BASE + 0x101, bytes([0x00]))  # hi 0 => JP source

    # Pattern 1 / 2 pointers ($1A00/$1A80), bodies abutting from PAT_ADDR.
    _put(image, DATA_BASE + 0x200 + 1, bytes([PAT_ADDR & 0xFF, PAT2_ADDR & 0xFF]))
    _put(image, DATA_BASE + 0x280 + 1, bytes([PAT_ADDR >> 8, PAT2_ADDR >> 8]))

    # Arranger V1 plays patterns 1 and 2, then a jump ($FF) terminator.
    _put(image, DATA_BASE + 0x300, bytes([0x01, 0x02, 0xFF]))

    # DL byte for sidTAB row 0.
    _put(image, DATA_BASE + 0x600, bytes([0x05]))

    # sidTAB row-0 compacted body: low bitmap WGh ($40) with a ctrl value.
    _put(image, ROW_ADDR, bytes([0x40, 0x41, 0x00]))

    if with_pattern:
        _put(image, PAT_ADDR, _PAT1_PACKED + _PAT2_PACKED)

    header = _psid_header(0x7C)
    return bytes(header + image)


class TestSidReplayReader(unittest.TestCase):
    def test_find_signature(self):
        image = SidImage.from_bytes(_synthetic_replay())
        self.assertEqual(find_signature(image.mem), SIG_ADDR)

    def test_find_signature_absent(self):
        # A bare image with no signature.
        self.assertEqual(find_signature(bytearray(0x10000)), -1)

    def test_depack_reconstructs_editor_layout(self):
        image = SidImage.from_bytes(_synthetic_replay())
        snap = depack_replay(image)
        self.assertIsNotNone(snap)
        song = DefmonSong(snap)
        # Arranger V1 step 0 references pattern 1.
        self.assertEqual(bytes(song.arranger_v1)[:3], bytes([0x01, 0x02, 0xFF]))
        # Pattern 1 decodes to 32 events; first is a note-on, later an ALT.
        events = song.pattern_events(1)
        self.assertEqual(len(events), 32)
        self.assertTrue(any(e.flag & 0x80 for e in events))
        # sidTAB row 0 relocated to $5F00 with the WGh ctrl value.
        row = song.sidtab_row(0)
        self.assertEqual(row.WGh, 0x41)
        # DL byte carried over.
        self.assertEqual(bytes(song.sidtab_dl)[0], 0x05)

    def test_packed_pattern_bodies_are_variable_length(self):
        """Bodies are flag + one byte per set gate bit, not fixed 4-byte records.

        The player advances its stream pointer by exactly the bytes it consumed
        ($11BD: ``TYA / CLC / ADC $1186 / STA $1186``), so a fixed-stride decode
        misreads every field after the first partial record and runs off the end
        of the body into the next pattern's.
        """
        song = DefmonSong(depack_replay(SidImage.from_bytes(_synthetic_replay())))
        for pat, expected in ((1, _PAT1_EVENTS), (2, _PAT2_EVENTS)):
            events = song.pattern_events(pat)
            self.assertEqual(len(events), 32, pat)
            decoded = tuple(
                (e.flag, e.slot_a, e.slot_b, e.note) for e in events[: len(expected)]
            )
            self.assertEqual(decoded, expected, f"pattern {pat}")
            # Nothing beyond the ALT step, and no note byte with bit 7 set.
            self.assertTrue(all(e.alt for e in events[len(expected) - 1 :][:1]))
            self.assertTrue(all(not e.note & 0x80 for e in events))
            self.assertEqual(
                bytes(song.pattern(pat))[len(expected) * 4 :],
                bytes(0x80 - len(expected) * 4),
                f"pattern {pat} overran past its ALT",
            )

    def test_depack_no_pattern_data_returns_none(self):
        image = SidImage.from_bytes(_synthetic_replay(with_pattern=False))
        self.assertIsNone(depack_replay(image))

    def test_depack_non_replay_returns_none(self):
        # PSID with no signature anywhere.
        header = _psid_header(0x7C)
        self.assertIsNone(
            depack_replay(SidImage.from_bytes(bytes(header + bytearray(0x800))))
        )

    def test_public_reader_dispatches_on_magic(self):
        raw = _synthetic_replay()
        # DefmonSong.from_bytes and DefmonSidParser.read both take the .sid path.
        self.assertEqual(
            len(DefmonSong.from_bytes(raw).snapshot), len(DefmonSong().snapshot)
        )
        self.assertIsInstance(DefmonSidParser().parse(raw), DefmonSong)

    def test_recognize_and_detect(self):
        raw = _synthetic_replay()
        parser = DefmonSidParser()
        image = SidImage.from_bytes(raw)
        self.assertEqual(parser.recognize(image), SIG_ADDR)
        detection = parser.detect(raw)
        self.assertEqual(detection.anchor, SIG_ADDR)

    def test_variant_replay_raises_specific_error(self):
        raw = _synthetic_replay(with_pattern=False)
        with self.assertRaises(DefmonError) as ctx:
            DefmonSong.from_sid_bytes(raw)
        self.assertIn("packer variant", str(ctx.exception))

    def test_non_replay_psid_raises(self):
        header = _psid_header(0x7C)
        raw = bytes(header + bytearray(0x800))
        with self.assertRaises(DefmonError) as ctx:
            DefmonSong.from_sid_bytes(raw)
        self.assertIn("signature not found", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
