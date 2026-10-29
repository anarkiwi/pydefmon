"""The ``streams.lark`` byte grammar for pattern streams and sidTAB rows."""

import unittest

from pysidtracker import SidImage, SidParseError

from pydefmon import SidtabRow
from pydefmon._layout import (
    STREAMS,
    EditorPattern,
    PackedPattern,
    RowColumns,
    store_pattern,
)

_STEPS = ((0x51, 5, 0, 0x31), (0x02, 0, 0, 0), (0x63, 7, 8, 0), (0x83, 0, 0, 0))


class TestPatternStreams(unittest.TestCase):
    def test_packed_reads_one_payload_byte_per_gate(self):
        data = bytes([0x51, 5, 0x31, 0x02, 0x63, 7, 8, 0x83, 0xAA, 0xBB])
        parsed = STREAMS.parse_prefix(data, "packed")
        self.assertEqual(parsed, (PackedPattern(_STEPS), 8))

    def test_editor_reads_fixed_four_byte_steps(self):
        data = EditorPattern(_STEPS).to_bytes() + b"\xff" * 8
        self.assertEqual(STREAMS.parse_prefix(data, "editor").size, 16)

    def test_an_alt_less_stream_ends_after_32_steps(self):
        for rule, step in (("packed", b"\x11\x01"), ("editor", b"\x11\x01\x02\x03")):
            with self.subTest(rule=rule):
                parsed = STREAMS.parse_prefix(step * 40, rule)
                self.assertEqual((len(parsed.model), parsed.size), (32, 32 * len(step)))

    def test_every_flag_byte_round_trips_in_both_forms(self):
        streams = []
        for flag in range(256):
            step = (flag, flag & 0x40 and 1, flag & 0x20 and 2, flag & 0x10 and 3)
            steps = [step] if flag & 0x80 else [step, (0x80, 0, 0, 0)]
            for form, rule in ((PackedPattern, "packed"), (EditorPattern, "editor")):
                streams.append((f"{rule} ${flag:02x}", form(steps), rule))
        STREAMS.check(streams, AssertionError)

    def test_a_stream_cut_short_raises(self):
        with self.assertRaises(SidParseError):
            STREAMS.parse_exact(bytes([0x70, 1]), "packed")

    def test_store_pattern_expands_a_packed_stream(self):
        mem = bytearray(0x10000)
        mem[0x4000:0x4008] = PackedPattern(_STEPS).to_bytes()
        image = SidImage(mem, 0x4000, 0x4008, None, bytes(mem), b"")
        snap = bytearray(0x5800)
        store_pattern(snap, image, 1, 0x4000)
        self.assertEqual(bytes(snap[0x780:0x790]), EditorPattern(_STEPS).to_bytes())

    def test_listing_names_each_byte(self):
        lines = list(STREAMS.listing([("p", PackedPattern(_STEPS), "packed")]))
        self.assertEqual(lines[0], "p:")
        self.assertIn("SAN=$51", "\n".join(lines))


class TestRowGrammar(unittest.TestCase):
    """``row``: each bitmap's set bits select the column values after it."""

    def test_columns_follow_in_player_order(self):
        raw = bytes([0xC2, 0x41, 0x01, 0x08, 0x88, 0x10, 0x50, 0xC0]) + bytes(7)
        model, size = STREAMS.parse_prefix(raw, "row")
        self.assertEqual(size, 8)
        self.assertEqual(
            model[2],
            (
                ("WGh", 0x41),
                ("WGl", 0x01),
                ("PW", 0x08),
                ("PS", 0x10),
                ("ACID", 0xC050),
            ),
        )

    def test_bits_the_player_does_not_test_select_nothing(self):
        model, size = STREAMS.parse_prefix(bytes([0x01, 0x07, 0xAA]), "row")
        self.assertEqual((model, size), (RowColumns((0x01, 0x07, ())), 2))

    def test_every_bitmap_round_trips(self):
        streams = []
        for low in range(256):
            for high in (0x00, 0xF8, low & 0xF8):
                values = bytes(range(1, 16))
                raw = bytes([low]) + values[: bin(low & 0xFE).count("1")]
                raw += (
                    bytes([high])
                    + values[: bin(high & 0xF0).count("1") + 2 * bool(high & 8)]
                )
                model = STREAMS.parse_prefix(raw, "row").model
                streams.append((f"row {low:02x}/{high:02x}", model, "row"))
        STREAMS.check(streams, AssertionError)

    def test_sidtab_row_reads_and_writes_through_the_grammar(self):
        raw = RowColumns.of({"AD": 9, "ACID": 0x1234}).to_bytes() + b"\x77"
        row = SidtabRow.parse(3, raw.ljust(15, b"\0"))
        self.assertEqual((row.AD, row.ACID, row.trailing[:1]), (9, 0x1234, b"\x77"))
        self.assertEqual(row.to_bytes(), raw.ljust(15, b"\0"))


if __name__ == "__main__":
    unittest.main()
