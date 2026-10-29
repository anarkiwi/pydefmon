"""The ``patterns.lark`` byte grammar for editor and packed pattern streams."""

import unittest

from pysidtracker import SidImage, SidParseError

from pydefmon._layout import (
    PATTERN_STREAMS,
    EditorPattern,
    PackedPattern,
    store_pattern,
)

_STEPS = ((0x51, 5, 0, 0x31), (0x02, 0, 0, 0), (0x63, 7, 8, 0), (0x83, 0, 0, 0))


class TestPatternStreams(unittest.TestCase):
    def test_packed_reads_one_payload_byte_per_gate(self):
        data = bytes([0x51, 5, 0x31, 0x02, 0x63, 7, 8, 0x83, 0xAA, 0xBB])
        parsed = PATTERN_STREAMS.parse_prefix(data, "packed")
        self.assertEqual(parsed, (PackedPattern(_STEPS), 8))

    def test_editor_reads_fixed_four_byte_steps(self):
        data = EditorPattern(_STEPS).to_bytes() + b"\xff" * 8
        self.assertEqual(PATTERN_STREAMS.parse_prefix(data, "editor").size, 16)

    def test_an_alt_less_stream_ends_after_32_steps(self):
        for rule, step in (("packed", b"\x11\x01"), ("editor", b"\x11\x01\x02\x03")):
            with self.subTest(rule=rule):
                parsed = PATTERN_STREAMS.parse_prefix(step * 40, rule)
                self.assertEqual((len(parsed.model), parsed.size), (32, 32 * len(step)))

    def test_every_flag_byte_round_trips_in_both_forms(self):
        streams = []
        for flag in range(256):
            step = (flag, flag & 0x40 and 1, flag & 0x20 and 2, flag & 0x10 and 3)
            steps = [step] if flag & 0x80 else [step, (0x80, 0, 0, 0)]
            for form, rule in ((PackedPattern, "packed"), (EditorPattern, "editor")):
                streams.append((f"{rule} ${flag:02x}", form(steps), rule))
        PATTERN_STREAMS.check(streams, AssertionError)

    def test_a_stream_cut_short_raises(self):
        with self.assertRaises(SidParseError):
            PATTERN_STREAMS.parse_exact(bytes([0x70, 1]), "packed")

    def test_store_pattern_expands_a_packed_stream(self):
        mem = bytearray(0x10000)
        mem[0x4000:0x4008] = PackedPattern(_STEPS).to_bytes()
        image = SidImage(mem, 0x4000, 0x4008, None, bytes(mem), b"")
        snap = bytearray(0x5800)
        store_pattern(snap, image, 1, 0x4000)
        self.assertEqual(bytes(snap[0x780:0x790]), EditorPattern(_STEPS).to_bytes())

    def test_listing_names_each_byte(self):
        lines = list(PATTERN_STREAMS.listing([("p", PackedPattern(_STEPS), "packed")]))
        self.assertEqual(lines[0], "p:")
        self.assertIn("SAN=$51", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
