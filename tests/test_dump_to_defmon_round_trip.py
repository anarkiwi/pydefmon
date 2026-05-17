"""Pin ``dump_to_defmon`` round-trip output against the glow_worm baseline.

Two gates:

1. **Algo regression gate.** ``build_song`` on the canonical source CSV
   (``fixtures/glow_worm.csv``) must produce bytes byte-identical to
   the committed recon .prg (``glow_worm.recon.baseline.prg``). Catches
   any drift in the snippet+JP algorithm without needing pyresidfp /
   docker.

2. **Recall pin.** The comparator at
   ``profile.dump_to_defmon_recall`` on (source CSV, baseline recon CSV)
   must report the documented overall-recall figure. Catches comparator
   regressions and provides a static anchor for the per-register-class
   numbers cited in AGENTS.md.

When ``dump_to_defmon.py`` is intentionally improved:

  1. Run ``tools/run_dump_to_defmon_round_trip.sh`` (docker; renders
     fresh recon .prg + .csv).
  2. Compare new recall vs ``EXPECTED_OVERALL_MATCHED``; audit audibly
     via the rendered WAV.
  3. If accepted, regenerate fixtures + bump
     ``EXPECTED_OVERALL_MATCHED`` in this file in the same commit.
"""

import os
import sys
import unittest

from pydefmon.dump_to_defmon import build_song

# Profile dir is a sibling of tests/; add the repo root to sys.path so
# we can import the comparator without forcing pip-install.
REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
from profile.dump_to_defmon_recall import compute_recall  # noqa: E402

FIXTURE_DIR = os.path.join(REPO_ROOT, "fixtures")
SOURCE_CSV = os.path.join(FIXTURE_DIR, "glow_worm.csv")
BASELINE_PRG = os.path.join(FIXTURE_DIR, "glow_worm.recon.baseline.prg")
BASELINE_RECON_CSV = os.path.join(FIXTURE_DIR, "glow_worm.recon.baseline.csv")

# Pinned baseline progression:
#   832  - initial snippet+JP MVP
#   832  - cross-voice merge + pattern dedup (byte-neutral)
#   1079 - slide reverse-mapping, note-only boundary
#   1215 - option B: gate-bit boundary + slide-continuation merge
#   1468 - PS sweep reverse-mapping
#   2074 - trigger-placement fix: gap=3 triggers via dur=1
#   2586 - layer-1 ACID cascade: cutoff drive on 7/16 reset frames,
#          cutoff recall 0.06% -> 32%
EXPECTED_SRC_ROWS = 14236
EXPECTED_OVERALL_MATCHED = 2586


class TestBuildSongBytewise(unittest.TestCase):
    def test_build_song_matches_baseline_prg(self):
        """``build_song(source_csv).to_bytes()`` is byte-identical to the
        committed recon .prg. Pins the snippet+JP algorithm."""
        song = build_song(SOURCE_CSV, verbose=False)
        actual = song.to_bytes()
        with open(BASELINE_PRG, "rb") as f:
            expected = f.read()
        self.assertEqual(
            len(actual),
            len(expected),
            "snapshot length drift -- algo change suspected",
        )
        self.assertEqual(actual, expected)


class TestRecallBaseline(unittest.TestCase):
    def test_overall_recall_pinned(self):
        rep = compute_recall(SOURCE_CSV, BASELINE_RECON_CSV)
        self.assertEqual(rep.src_rows, EXPECTED_SRC_ROWS)
        self.assertEqual(rep.overall_matched, EXPECTED_OVERALL_MATCHED)


if __name__ == "__main__":
    unittest.main()
