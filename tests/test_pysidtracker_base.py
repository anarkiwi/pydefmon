"""pysidtracker shared-base integration: error hierarchy re-parenting and the
:class:`DefmonSidParser` API-consistency smoke test."""

import unittest

from pysidtracker import BaseSidParser, SidError

from pydefmon.defmon import DefmonError, DefmonSidParser, DefmonSong
from pydefmon._load_format import CodecError

from tests._support import fixture_path


def _glow_worm_or_skip(test):
    try:
        return fixture_path("glow_worm.prg")
    except FileNotFoundError as e:
        test.skipTest(str(e))
        return None


class TestErrorHierarchy(unittest.TestCase):
    def test_defmon_error_is_sid_error_and_value_error(self):
        self.assertTrue(issubclass(DefmonError, SidError))
        self.assertTrue(issubclass(DefmonError, ValueError))

    def test_codec_error_is_sid_error_and_value_error(self):
        self.assertTrue(issubclass(CodecError, SidError))
        self.assertTrue(issubclass(CodecError, ValueError))

    def test_defmon_error_raises_as_value_error(self):
        with self.assertRaises(ValueError):
            DefmonSong.from_bytes(b"\x00\x18")
        with self.assertRaises(SidError):
            DefmonSong.from_bytes(b"\x00\x18")


class TestDefmonSidParser(unittest.TestCase):
    def test_is_base_sid_parser(self):
        self.assertIsInstance(DefmonSidParser(), BaseSidParser)
        self.assertIs(DefmonSidParser.error_class, DefmonError)

    def test_read_matches_from_bytes(self):
        path = _glow_worm_or_skip(self)
        with open(path, "rb") as f:
            raw = f.read()
        expected = DefmonSong.from_bytes(raw)
        got = DefmonSidParser().read(path)
        self.assertIsInstance(got, DefmonSong)
        self.assertEqual(bytes(got.snapshot), bytes(expected.snapshot))

    def test_recognize_returns_none(self):
        parser = DefmonSidParser()
        image = parser.load_image(b"\x00\x18" + b"\x00" * 16)
        self.assertIsNone(parser.recognize(image))


if __name__ == "__main__":
    unittest.main()
