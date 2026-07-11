"""Unit tests for the ``pydefmon.defmon`` module.

Tests that exercise the .GLOW WORM fixture skip when the fixture is
not present — run ``python -m tools.fetch_fixtures`` to populate.
"""

import unittest

from pydefmon.defmon import (
    DefmonError,
    DefmonSong,
    LOAD_ADDRESS,
    PatternEvent,
    SidcallFrame,
    SidtabRow,
    STANDARD_SNAPSHOT_SIZE,
)

from tests._support import fixture_path


def _glow_worm_or_skip(test):
    try:
        return fixture_path("glow_worm.prg")
    except FileNotFoundError as e:
        test.skipTest(str(e))
        return None


class TestDefmonSongRoundTrip(unittest.TestCase):
    def setUp(self):
        self.song = DefmonSong.from_file(_glow_worm_or_skip(self))

    def test_snapshot_size(self):
        self.assertEqual(len(self.song.snapshot), STANDARD_SNAPSHOT_SIZE)

    def test_load_address(self):
        self.assertEqual(self.song.load_address, LOAD_ADDRESS)

    def test_encode_idempotent(self):
        """``to_bytes`` is deterministic, so encode -> decode -> encode
        produces the same PRG bytes as the first encode."""
        once = self.song.to_bytes()
        twice = DefmonSong.from_bytes(once).to_bytes()
        self.assertEqual(once, twice)

    def test_pattern_pointer_table_zeroed_on_disk(self):
        self.assertEqual(bytes(self.song.pattern_pointer_table), b"\x00" * 0x100)


class TestParseErrors(unittest.TestCase):
    def test_short_file(self):
        with self.assertRaises(DefmonError):
            DefmonSong.from_bytes(b"\x00\x18")

    def test_wrong_load_address(self):
        with self.assertRaises(DefmonError):
            DefmonSong.from_bytes(b"\x01\x08" + b"\x00" * 32)

    def test_set_jump_count_validates(self):
        song = DefmonSong()
        with self.assertRaises(ValueError):
            song.set_jump(0, target=0, count=300)


class TestPatternAccessors(unittest.TestCase):
    def setUp(self):
        self.song = DefmonSong.from_file(_glow_worm_or_skip(self))

    def test_pattern_index_bounds(self):
        with self.assertRaises(IndexError):
            self.song.pattern(-1)
        with self.assertRaises(IndexError):
            self.song.pattern(128)

    def test_pattern_events_returns_32(self):
        events = self.song.pattern_events(0x1C)
        self.assertEqual(len(events), 32)
        for ev in events:
            self.assertIsInstance(ev, PatternEvent)

    def test_pattern_events_round_trip_to_bytes(self):
        for idx in (0, 1, 0x1C, 0x7F):
            original = bytes(self.song.pattern(idx))
            events = self.song.pattern_events(idx)
            rebuilt = b"".join(ev.to_bytes() for ev in events)
            self.assertEqual(rebuilt, original, f"pattern {idx}")

    def test_set_pattern_events_rejects_wrong_count(self):
        events = self.song.pattern_events(0)
        with self.assertRaises(ValueError):
            self.song.set_pattern_events(0, events[:31])


class TestPatternEventFactories(unittest.TestCase):
    def test_note_on_validates(self):
        with self.assertRaises(ValueError):
            PatternEvent.note_on(0)
        with self.assertRaises(ValueError):
            PatternEvent.note_on(0x80)
        with self.assertRaises(ValueError):
            PatternEvent.note_on(0x25, duration=16)

    def test_delay_to_bytes(self):
        ev = PatternEvent.delay(15)
        self.assertEqual(ev.to_bytes(), b"\x0f\x00\x00\x00")
        self.assertEqual(ev.duration, 15)
        self.assertFalse(ev.gate_n)

    def test_alt_end(self):
        ev = PatternEvent.alt_end(duration=4)
        self.assertTrue(ev.alt)
        self.assertEqual(ev.duration, 4)

    def test_silent_pattern_template(self):
        events = PatternEvent.silent_pattern()
        self.assertEqual(len(events), 32)
        self.assertTrue(events[-1].alt)

    def test_frequency_hz(self):
        # note=0x25 → NOTE_PITCH[0x25] = 0x024E = 590 → ~34.65 Hz at PAL.
        # The previous expectation (32.71 Hz) reflected an off-by-one
        # in the now-removed ``_NOTE_FREQ_WORDS`` LUT — the player has
        # always read ``NOTE_PITCH_LO/HI[note]`` directly, not
        # ``[note - 1]``.
        ev = PatternEvent(0x10, 0, 0, 0x25)
        self.assertAlmostEqual(ev.frequency_hz(), 34.65, places=1)

    def test_sid_freq_word_matches_player_tables(self):
        """``sid_freq_word()`` must read the same NOTE_PITCH bytes
        that the player walks at runtime — otherwise format-side
        analysis disagrees with what the chip will actually latch."""
        from pydefmon.defmon import NOTE_PITCH_HI, NOTE_PITCH_LO

        for note in range(1, 128):
            expected = (NOTE_PITCH_HI[note] << 8) | NOTE_PITCH_LO[note]
            self.assertEqual(
                PatternEvent(0x10, 0, 0, note).sid_freq_word(),
                expected,
                msg=f"note=0x{note:02X}",
            )

    def test_note_name_special_cases(self):
        self.assertEqual(PatternEvent(0, 0, 0, 0).note_name(), "---")
        self.assertEqual(PatternEvent(0, 0, 0, 0x80).note_name(), "$80")

    def test_byte_clamp_on_construction(self):
        ev = PatternEvent(0x100, 0x100, 0x100, 0x100)
        self.assertEqual(ev.to_bytes(), b"\x00\x00\x00\x00")

    def test_equality_and_hash(self):
        a = PatternEvent.parse(b"\x51\x18\x00\x30")
        b = PatternEvent.parse(b"\x51\x18\x00\x30")
        self.assertEqual(a, b)
        self.assertEqual(hash(a), hash(b))

    def test_parse_rejects_wrong_length(self):
        with self.assertRaises(ValueError):
            PatternEvent.parse(b"\x00\x00\x00")


class TestSidtabRow(unittest.TestCase):
    def test_low_only_row(self):
        row = SidtabRow.parse(0, bytes([0x40, 0x5A]) + b"\x00" * 13)
        self.assertEqual(row.values(), {"WGh": 0x5A})

    def test_acid_two_bytes(self):
        row = SidtabRow.parse(0, bytes([0x00, 0x08, 0x50, 0xC0]) + b"\x00" * 11)
        self.assertEqual(row.values(), {"ACID": 0x50C0})

    def test_parse_rejects_wrong_length(self):
        with self.assertRaises(ValueError):
            SidtabRow.parse(0, b"\x00" * 14)


class TestSidtabRowPacker(unittest.TestCase):
    """Inverse of SidtabRow.parse: SidtabRow.to_bytes / .pack must
    re-emit the same 15-byte form the parser consumed."""

    def test_round_trip_low_only(self):
        raw = bytes([0x40, 0x5A]) + b"\x00" * 13
        self.assertEqual(SidtabRow.parse(0, raw).to_bytes(), raw)

    def test_round_trip_low_packed(self):
        raw = bytes([0xD1, 0x11, 0x22, 0x33]) + b"\x00" * 11
        # Bit 0 of low_bitmap is "active flag" / unused -- the parser
        # ignores it. Packing won't re-set it, so the round-trip
        # canonicalises bit 0 to 0.
        canonical = bytes([0xD0, 0x11, 0x22, 0x33]) + b"\x00" * 11
        self.assertEqual(SidtabRow.parse(0, raw).to_bytes(), canonical)

    def test_round_trip_acid_two_bytes(self):
        raw = bytes([0x00, 0x08, 0x50, 0xC0]) + b"\x00" * 11
        self.assertEqual(SidtabRow.parse(0, raw).to_bytes(), raw)

    def test_round_trip_empty_row(self):
        raw = b"\x00" * 15
        self.assertEqual(SidtabRow.parse(0, raw).to_bytes(), raw)

    def test_round_trip_trailing_preserved(self):
        raw = bytes([0x40, 0x5A, 0x00, 0xAA, 0xBB]) + b"\x00" * 10
        self.assertEqual(SidtabRow.parse(0, raw).to_bytes(), raw)

    def test_pack_classmethod(self):
        packed = SidtabRow.pack({"WGh": 0x5A})
        self.assertEqual(packed[0], 0x40)
        self.assertEqual(packed[1], 0x5A)
        self.assertEqual(packed[2], 0)
        self.assertEqual(len(packed), 15)

    def test_pack_acid_high_only(self):
        packed = SidtabRow.pack({"ACID": 0x50C0})
        self.assertEqual(packed[0], 0)
        self.assertEqual(packed[1], 0x08)
        self.assertEqual(packed[2], 0x50)
        self.assertEqual(packed[3], 0xC0)

    def test_pack_unknown_column_raises(self):
        with self.assertRaises(ValueError):
            SidtabRow.pack({"NOPE": 0x42})

    def test_pack_oversized_trailing_raises(self):
        with self.assertRaises(ValueError):
            SidtabRow.pack({"WGh": 0x5A}, trailing=b"\x00" * 14)

    def test_pack_clamps_byte_values(self):
        packed = SidtabRow.pack({"WGh": 0x1FF})
        self.assertEqual(packed[1], 0xFF)


class TestBuilders(unittest.TestCase):
    def setUp(self):
        self.song = DefmonSong.from_file(_glow_worm_or_skip(self))

    def test_clear_song_table(self):
        self.song.clear_song_table()
        self.assertEqual(bytes(self.song.voice_pattern_refs), b"\x00" * 0x400)

    def test_clear_arranger_preserves_dl(self):
        # Plant a DL byte we can check after the arranger wipe.
        self.song.set_dl(7, 0x42)
        self.song.clear_arranger()
        # Arranger arrays ($1B00..$1DFF) zeroed.
        self.assertEqual(bytes(self.song.voice_pattern_refs[:0x300]), b"\x00" * 0x300)
        # DL byte at $1E07 still intact.
        self.assertEqual(self.song.snapshot[0x1E00 - LOAD_ADDRESS + 7], 0x42)

    def test_clear_dl_preserves_arranger(self):
        self.song.clear_arranger()
        self.song.set_step(3, v1=5)
        self.song.set_dl(7, 0x42)
        self.song.clear_dl()
        # Arranger entry at step 3 still intact.
        self.assertEqual(self.song.snapshot[0x1B00 - LOAD_ADDRESS + 3], 5)
        # DL region wiped.
        self.assertEqual(
            bytes(self.song.voice_pattern_refs[0x300:0x400]), b"\x00" * 0x100
        )

    def test_set_step_validates(self):
        with self.assertRaises(ValueError):
            self.song.set_step(256, v1=1)
        with self.assertRaises(ValueError):
            self.song.set_step(0, v1=0x80)

    def test_set_jump_validates(self):
        with self.assertRaises(ValueError):
            self.song.set_jump(256, target=0)
        with self.assertRaises(ValueError):
            self.song.set_jump(0, target=300)

    def test_authoring_round_trip(self):
        self.song.clear_song_table()
        self.song.set_step(0, v1=1)
        self.song.set_jump(1, target=0)
        events = [PatternEvent.note_on(0x25, slot_a=0x08, duration=15)]
        events += [PatternEvent.delay(15) for _ in range(30)]
        events += [PatternEvent.alt_end()]
        self.song.set_pattern_events(1, events)
        rebuilt = DefmonSong.from_bytes(self.song.to_bytes())
        rb_vpr = bytes(rebuilt.voice_pattern_refs)
        self.assertEqual(rb_vpr[0], 1)
        # $1B00,y = $FF is the arranger's jump marker (see
        # _arranger_advance in defmon_player).
        self.assertEqual(rb_vpr[1], 0xFF)
        rb_events = rebuilt.pattern_events(1)
        self.assertEqual(rb_events[0].note, 0x25)
        self.assertTrue(rb_events[31].alt)


class TestOptimize(unittest.TestCase):
    """``DefmonSong.optimize()`` must shrink resource usage but never
    change the rendered SID register stream."""

    @staticmethod
    def _build_duplicate_song():
        """Two sidtab rows with identical content + two patterns with
        identical bodies, each routed to a separate voice. After
        optimize, only the lowest-indexed copy in each pair should
        still be referenced."""
        from pydefmon.defmon import SidtabRow

        song = DefmonSong()
        # Two identical sidtab rows at 1 and 5 (STop'd).
        cols = {"WGh": 0x41, "AD": 0x09, "SR": 0xA0, "PW": 0x80}
        raw = SidtabRow.pack(cols)
        for row_idx in (1, 5):
            off = 0x5F00 - LOAD_ADDRESS + row_idx * 15
            for i, b in enumerate(raw):
                song.snapshot[off + i] = b
        # clear_song_table BEFORE set_dl (DL region is wiped by it).
        song.clear_song_table()
        for row_idx in (1, 5):
            song.set_dl(row_idx, 0xFF)
            song.set_jp(row_idx)
        # Two identical patterns at 1 and 2, each fires its own slot.
        events_1 = (
            [PatternEvent.note_on(0x18, slot_a=1, duration=0)]
            + [PatternEvent.delay(0) for _ in range(30)]
            + [PatternEvent.alt_end(duration=0)]
        )
        events_2 = (
            [PatternEvent.note_on(0x18, slot_a=5, duration=0)]
            + [PatternEvent.delay(0) for _ in range(30)]
            + [PatternEvent.alt_end(duration=0)]
        )
        song.set_pattern_events(1, events_1)
        # Pattern 2 = pattern 1 except slot_a=5 → becomes identical after
        # sidtab dedup remaps 5→1 in pattern 2's events.
        song.set_pattern_events(2, events_2)
        # Arranger: V0=pat 1, V1=pat 2. Both will play the same note
        # with the same instrument; after optimize, V1 should
        # reference pattern 1 instead of pattern 2.
        song.set_step(0, v1=1, v2=2, v3=0)
        song.set_jump(1, target=0, count=0)
        return song

    def test_optimize_merges_duplicate_sidtab_rows(self):
        song = self._build_duplicate_song()
        song.optimize()
        # After sidtab dedup, the slot_a=5 references in pattern 2 were
        # rewritten to slot_a=1 (and via pattern dedup, pattern 2 itself
        # collapses to pattern 1).
        ev = song.pattern_events(1)[0]
        self.assertEqual(ev.slot_a, 1)
        ev2 = song.pattern_events(2)[0]
        self.assertEqual(ev2.slot_a, 1)

    def test_optimize_merges_duplicate_patterns(self):
        song = self._build_duplicate_song()
        song.optimize()
        # V1 arranger entry at step 0 was pattern 2, now should be
        # pattern 1 after dedup.
        v1_entry = song.snapshot[0x1C00 - LOAD_ADDRESS + 0]
        self.assertEqual(v1_entry, 1)

    def test_optimize_is_idempotent(self):
        song = self._build_duplicate_song()
        once = bytes(song.optimize().snapshot)
        twice = bytes(song.optimize().snapshot)
        self.assertEqual(once, twice)

    def test_optimize_does_not_merge_non_stop_rows(self):
        """A row with DL < $80 advances to the next row, so its index
        is load-bearing — optimize must leave it alone even if a later
        STop'd row has identical content."""
        from pydefmon.defmon import SidtabRow

        song = DefmonSong()
        cols = {"WGh": 0x41, "AD": 0x09, "SR": 0xA0, "PW": 0x80}
        raw = SidtabRow.pack(cols)
        for row_idx in (3, 7):
            off = 0x5F00 - LOAD_ADDRESS + row_idx * 15
            for i, b in enumerate(raw):
                song.snapshot[off + i] = b
        song.clear_song_table()
        song.set_dl(3, 0x00)  # advance (non-STop)
        song.set_jp(3)
        song.set_dl(7, 0xFF)  # STop
        song.set_jp(7)
        events = (
            [PatternEvent.note_on(0x18, slot_a=3, duration=0)]
            + [PatternEvent.delay(0) for _ in range(30)]
            + [PatternEvent.alt_end(duration=0)]
        )
        song.set_pattern_events(1, events)
        song.set_step(0, v1=1)
        song.optimize()
        # slot_a=3 must remain (row 3's index is interior to its own
        # cascade — advancing to row 4, etc.).
        self.assertEqual(song.pattern_events(1)[0].slot_a, 3)

    def test_optimize_skips_jump_arranger_rows(self):
        """When V0[idx]=$FF (jump row), V1[idx] and V2[idx] are
        target/count, not pattern indices — optimize must not rewrite
        them."""
        song = DefmonSong()
        song.set_pattern_events(0, PatternEvent.silent_pattern())
        events = (
            [PatternEvent.note_on(0x18, slot_a=1, duration=0)]
            + [PatternEvent.delay(0) for _ in range(30)]
            + [PatternEvent.alt_end(duration=0)]
        )
        song.set_pattern_events(1, events)
        song.set_pattern_events(2, events)  # duplicate of pattern 1
        song.clear_song_table()
        song.set_step(0, v1=1)
        # set_jump writes V0=0xFF, V1=target=0, V2=count=0.
        song.set_jump(1, target=0, count=0)
        # Manually plant V2=2 on the jump row to verify it's NOT
        # rewritten by pattern dedup (since on a jump row V2 is "count"
        # not a pattern index).
        song.snapshot[0x1D00 - LOAD_ADDRESS + 1] = 2
        song.optimize()
        # V2 byte on the jump row stays as 2 (count), even though
        # pattern 2 is a dedup victim.
        self.assertEqual(song.snapshot[0x1D00 - LOAD_ADDRESS + 1], 2)


class TestSidtabJpDl(unittest.TestCase):
    """Exercises the per-sidTAB-row JP / DL accessors on ``DefmonSong``
    against the on-disk marker model documented in AGENTS.md. See the
    ``set_jp`` / ``set_dl`` / ``jp_target`` docstrings for the byte
    semantics."""

    def setUp(self):
        self.song = DefmonSong.from_file(_glow_worm_or_skip(self))

    def test_set_dl_validates(self):
        with self.assertRaises(ValueError):
            self.song.set_dl(256, 0)
        with self.assertRaises(ValueError):
            self.song.set_dl(0, 0x100)

    def test_set_jp_validates(self):
        with self.assertRaises(ValueError):
            self.song.set_jp(256, target=0)
        with self.assertRaises(ValueError):
            self.song.set_jp(0, target=300)

    def test_jp_target_validates(self):
        with self.assertRaises(ValueError):
            self.song.jp_target(256)

    def test_set_dl_writes_1e00(self):
        self.song.set_dl(7, 0x80)
        self.assertEqual(self.song.sidtab_dl[7], 0x80)
        # On-disk addressing: $1E00,Y is at snapshot offset (0x1E00 - 0x1800) + Y.
        self.assertEqual(self.song.snapshot[0x1E00 - LOAD_ADDRESS + 7], 0x80)

    def test_set_jp_linear_writes_11_marker(self):
        # Pre-condition: fixture has $1800,5 == $11 (the on-disk form). The
        # $D6C9 decoder writes the $11 marker to BOTH $1800,X and $1900,X
        # for active-linear rows; $CF42's post-LOAD pass then rewrites
        # both to the runtime pointer ($5F00 + X*$0F). The
        # ``unpacked_snapshot()`` view models that secondary pass;
        # ``song.snapshot`` is the pre-$CF42 form.
        self.assertEqual(self.song.snapshot[0x1800 - LOAD_ADDRESS + 5], 0x11)
        self.song.set_jp(5)
        self.assertEqual(self.song.sidtab_jp[5], 0x11)
        # set_jp(target=None) leaves $1800,Y untouched -- $CF42 rewrites both
        # bytes on LOAD anyway, so the paired value is don't-care at runtime.
        self.assertEqual(self.song.snapshot[0x1800 - LOAD_ADDRESS + 5], 0x11)
        self.assertIsNone(self.song.jp_target(5))

    def test_set_jp_target_writes_jump_source(self):
        self.song.set_jp(5, target=0x42)
        self.assertEqual(self.song.sidtab_jp[5], 0x00)
        self.assertEqual(self.song.snapshot[0x1800 - LOAD_ADDRESS + 5], 0x42)
        self.assertEqual(self.song.jp_target(5), 0x42)

    def test_active_linear_and_dl_round_trip(self):
        """Encode -> decode preserves active-linear JP markers and DL
        bytes."""
        self.song.set_jp(3)  # active linear -- writes $1900,3 = $11
        self.song.set_dl(3, 0x05)  # hold row 3 for 6 frames
        self.song.set_dl(7, 0x82)  # STop on row 7
        rebuilt = DefmonSong.from_bytes(self.song.to_bytes())
        self.assertEqual(rebuilt.sidtab_jp[3], 0x11)
        self.assertIsNone(rebuilt.jp_target(3))
        self.assertEqual(rebuilt.sidtab_dl[3], 0x05)
        self.assertEqual(rebuilt.sidtab_dl[7], 0x82)

    def test_jp_source_round_trip_through_to_bytes(self):
        """``set_jp(target=K) -> to_bytes() -> from_bytes()`` preserves
        the JP target. ``DefmonSong.to_bytes()`` does not zero
        ``$1800,Y`` on JP-source rows, so the low byte survives the
        $D6C9 round-trip."""
        self.song.set_jp(0x20, target=0x42)
        self.song.set_jp(0x21, target=0x55)
        self.song.set_dl(0x20, 0x05)
        rebuilt = DefmonSong.from_bytes(self.song.to_bytes())
        self.assertEqual(rebuilt.sidtab_jp[0x20], 0x00)
        self.assertEqual(rebuilt.jp_target(0x20), 0x42)
        self.assertEqual(rebuilt.sidtab_jp[0x21], 0x00)
        self.assertEqual(rebuilt.jp_target(0x21), 0x55)
        self.assertEqual(rebuilt.sidtab_dl[0x20], 0x05)

    def test_arranger_split_views(self):
        """The split arranger accessors ``arranger_v{1,2,3}`` expose
        the same bytes as the legacy ``voice_pattern_refs`` window."""
        vpr = bytes(self.song.voice_pattern_refs)
        self.assertEqual(bytes(self.song.arranger_v1), vpr[:0x100])
        self.assertEqual(bytes(self.song.arranger_v2), vpr[0x100:0x200])
        self.assertEqual(bytes(self.song.arranger_v3), vpr[0x200:0x300])
        # $1E00 (DL) lands at the tail of voice_pattern_refs by accident
        # of address layout, NOT because it's part of the arranger.
        self.assertEqual(bytes(self.song.sidtab_dl), vpr[0x300:0x400])


class TestSidcallFrameDl(unittest.TestCase):
    def test_dl_alias_matches_control(self):
        frame = SidcallFrame(
            row_index=4, sidtab_row=SidtabRow.parse(4, b"\x00" * 15), control=0x42
        )
        self.assertEqual(frame.dl, 0x42)
        self.assertEqual(frame.dl, frame.control)
        freeze = SidcallFrame(
            row_index=4, sidtab_row=SidtabRow.parse(4, b"\x00" * 15), control=0xFE
        )
        self.assertEqual(freeze.dl, 0xFE)
        self.assertTrue(freeze.freezes)


class TestSidtabRowJpDl(unittest.TestCase):
    """``SidtabRow`` carries optional ``jp`` and ``dl`` companion bytes
    -- the per-row state at ``$1900,index`` and ``$1E00,index`` that
    the cascade reads alongside the 15-byte row body."""

    def test_default_jp_dl_are_none(self):
        row = SidtabRow.parse(0, b"\x00" * 15)
        self.assertIsNone(row.jp)
        self.assertIsNone(row.dl)

    def test_parse_attaches_companion_bytes(self):
        row = SidtabRow.parse(7, b"\x00" * 15, jp=0x11, dl=0x82)
        self.assertEqual(row.jp, 0x11)
        self.assertEqual(row.dl, 0x82)

    def test_parse_validates_jp_dl_range(self):
        with self.assertRaises(ValueError):
            SidtabRow.parse(0, b"\x00" * 15, jp=0x100)
        with self.assertRaises(ValueError):
            SidtabRow.parse(0, b"\x00" * 15, dl=-1)

    def test_to_bytes_independent_of_companions(self):
        """``to_bytes`` returns the 15-byte row body; jp/dl live in
        separate RAM regions and are NOT part of the pack."""
        row_a = SidtabRow.parse(3, b"\x00" * 15)
        row_b = SidtabRow.parse(3, b"\x00" * 15, jp=0x11, dl=0x80)
        self.assertEqual(row_a.to_bytes(), row_b.to_bytes())

    def test_equality_includes_jp_dl(self):
        """Two rows with the same body but different jp/dl are NOT
        equal -- callers comparing rows pick up cascade-side context."""
        a = SidtabRow.parse(0, b"\x00" * 15, jp=0x11, dl=0x05)
        b = SidtabRow.parse(0, b"\x00" * 15, jp=0x11, dl=0x05)
        c = SidtabRow.parse(0, b"\x00" * 15, jp=0x00, dl=0x05)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(hash(a), hash(b))

    def test_repr_includes_jp_dl_when_set(self):
        row = SidtabRow.parse(2, b"\x00" * 15, jp=0x11, dl=0x82)
        text = repr(row)
        self.assertIn("jp=$11", text)
        self.assertIn("dl=$82", text)

    def test_repr_omits_companions_when_none(self):
        row = SidtabRow.parse(2, b"\x00" * 15)
        text = repr(row)
        self.assertNotIn("jp=", text)
        self.assertNotIn("dl=", text)


class TestDefmonSongSidtabRowCompanions(unittest.TestCase):
    """``DefmonSong.sidtab_row(i)`` populates ``jp`` and ``dl`` from
    the snapshot. ``sidcall_frames`` likewise passes them on the
    rows it returns."""

    def test_sidtab_row_pulls_companion_bytes(self):
        song = DefmonSong()
        song.set_jp(5, target=0x42)  # $1900,5 = $00, $1800,5 = $42
        song.set_dl(5, 0x80)  # $1E00,5 = $80
        row = song.sidtab_row(5)
        self.assertEqual(row.jp, 0x00)
        self.assertEqual(row.dl, 0x80)

    def test_sidtab_row_active_linear_marker(self):
        song = DefmonSong()
        song.set_jp(3)
        song.set_dl(3, 0x05)
        row = song.sidtab_row(3)
        self.assertEqual(row.jp, 0x11)
        self.assertEqual(row.dl, 0x05)


class TestReprAndEquality(unittest.TestCase):
    """Exercises the dunder methods (repr / eq / hash) which are otherwise
    only hit indirectly through assertion error messages."""

    def setUp(self):
        self.song = DefmonSong.from_file(_glow_worm_or_skip(self))

    def test_pattern_event_repr_with_gates(self):
        ev = PatternEvent.parse(b"\xd1\x1b\x00\x6c")
        r = repr(ev)
        self.assertIn("[", r)
        self.assertIn("dur=", r)

    def test_pattern_event_repr_no_gates(self):
        ev = PatternEvent.delay(0)
        self.assertIn("[-]", repr(ev))

    def test_sidcall_frame_hold_frames(self):
        frame = SidcallFrame(
            row_index=8, sidtab_row=SidtabRow.parse(8, b"\x00" * 15), control=0x03
        )
        self.assertEqual(frame.hold_frames, 4)
        self.assertFalse(frame.freezes)
        freeze = SidcallFrame(
            row_index=8, sidtab_row=SidtabRow.parse(8, b"\x00" * 15), control=0x80
        )
        self.assertIsNone(freeze.hold_frames)
        self.assertTrue(freeze.freezes)
        self.assertIn("FREEZE", repr(freeze))
        # Non-freeze frame with loops_to set picks the "loop@" branch in repr.
        looping = SidcallFrame(
            row_index=8, sidtab_row=SidtabRow.parse(8, b"\x00" * 15), control=0x03
        )
        looping.loops_to = 0x05
        self.assertIn("loop@", repr(looping))

    def test_defmon_song_repr(self):
        r = repr(self.song)
        self.assertIn("DefmonSong", r)
        self.assertIn("snapshot=", r)


if __name__ == "__main__":
    unittest.main()
