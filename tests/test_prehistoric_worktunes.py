"""Prehistoric 2008 defMON worktunes (raw $1700 saves), issue #39.

The 2008 builds save/LOAD editor worktunes raw at $1700 (no $D6C9 codec) in a
layout no surviving sample can verify; pydefmon rejects that $1700 container
with a specific error pinned to the 56-block build's KERNAL sites."""

import unittest

from pydefmon.defmon import (
    PREHISTORIC_LOAD_ADDRESS,
    PREHISTORIC_WORKTUNE_ERROR,
    DefmonParseError,
    DefmonSidParser,
    DefmonSong,
)

from tests._support import prehistoric_56block_image


def _prg(addr: int, size: int = 0x100) -> bytes:
    """A minimal raw PRG: 2-byte load address header + ``size`` zero bytes."""
    return addr.to_bytes(2, "little") + b"\x00" * size


class TestPrehistoricDetection(unittest.TestCase):
    """$1700 PRGs are rejected with the specific prehistoric error (offline)."""

    def test_1700_prg_raises_specific_error(self):
        with self.assertRaises(DefmonParseError) as captured:
            DefmonSong.from_bytes(_prg(PREHISTORIC_LOAD_ADDRESS))
        msg = str(captured.exception)
        self.assertIn("prehistoric 2008 defMON worktune", msg)
        self.assertIn("not supported", msg)
        self.assertIn(f"${PREHISTORIC_LOAD_ADDRESS:04X}", msg)
        self.assertNotIn("unexpected load address", msg)

    def test_error_is_the_named_constant(self):
        with self.assertRaises(DefmonParseError) as captured:
            DefmonSong.from_bytes(_prg(PREHISTORIC_LOAD_ADDRESS))
        self.assertEqual(str(captured.exception), PREHISTORIC_WORKTUNE_ERROR)

    def test_unrelated_address_keeps_the_generic_error(self):
        with self.assertRaises(DefmonParseError) as captured:
            DefmonSong.from_bytes(_prg(0x1500, 32))
        msg = str(captured.exception)
        self.assertIn("unexpected load address $1500", msg)
        self.assertNotIn("prehistoric", msg)

    def test_parser_adapter_surfaces_the_error(self):
        with self.assertRaises(DefmonParseError):
            DefmonSidParser().parse(_prg(PREHISTORIC_LOAD_ADDRESS))
        with self.assertRaises(ValueError):
            DefmonSong.from_bytes(_prg(PREHISTORIC_LOAD_ADDRESS))


class TestPrehistoricBinary(unittest.TestCase):
    """The $1700 base is what the real 2008 56-block build programs (skip offline)."""

    @classmethod
    def setUpClass(cls):
        cls.image = prehistoric_56block_image()
        if cls.image is None:
            raise unittest.SkipTest("2008 56-block build unreachable (offline)")

    def test_load_call_programs_1700(self):
        # $7200..$720B: A=sec-lo $00 / X=file $00 / Y=B=sec-hi $17, then JSR $FFD5 -> LOAD at $1700.
        self.assertEqual(
            self.image[0x7200:0x720C],
            bytes(
                [0xA9, 0x00, 0xA2, 0x00, 0xA0, 0x17, 0x20, 0xD5, 0xFF, 0xB0, 0x01, 0x60]
            ),
        )

    def test_save_call_programs_1700(self):
        # $7238..$7248: hi $17 latched at $FE, lo $00 at $FD, file/drive from $7804/$7805, JSR $FFD8 -> raw SAVE.
        self.assertEqual(
            self.image[0x7238:0x724B],
            bytes(
                [
                    0xA9,
                    0x00,
                    0x85,
                    0xFD,
                    0xA9,
                    0x17,
                    0x85,
                    0xFE,
                    0xAE,
                    0x04,
                    0x78,
                    0xAC,
                    0x05,
                    0x78,
                    0xA9,
                    0xFD,
                    0x20,
                    0xD8,
                    0xFF,
                ]
            ),
        )


if __name__ == "__main__":
    unittest.main()
