"""Byte-exact comparison of :class:`pydefmon.DefmonPlayer` against the oracle.

Marked ``oracle``: these tests need Docker (the ``anarkiwi/sidtrace`` image) and
network access to HVSC, so the default suite excludes them (see ``pyproject``); a
dedicated CI job runs ``pytest -m oracle``. They are never skipped -- an
unavailable tune or a failed oracle render fails the test rather than hiding a
regression. HVSC ``.sid`` files are copyright works: they are downloaded to a
cache (or a local ``$HVSC`` tree), never committed.

``DefmonPlayer`` runs each tune's own relocatable replay on a jennings 6502 (with the
NMOS illegal opcodes defMON uses), so every driver variation is exercised
directly against the sidtrace oracle. The TUNES set spans both replay-decode
families, multiple authors, both SID models and CIA multi-speed timing.
"""

import sys
from pathlib import Path

import pytest

from pysidtracker import make_oracle_fixtures, song_frames
from pysidtracker.oracle import aligned_match
from pysidtracker.testing import TuneFetchError, oracle_grid, resolve_tune

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import oracle_hvsc  # noqa: E402  (after sys.path tweak)

# HVSC defMON ``.sid`` replays verified byte-exact against the deterministic
# oracle. Covers both runtime replay variations pydefmon supports plus a spread
# of authors:
#   * standard defMON editor-layout replay -- Demsky, Ilkke, Dex-D;
#   * Goto80's older compact-runtime replay -- Rent-A-Cop;
#   * osc3/env3-driven cutoff on a 6581 and CIA multi-speed timing.
TUNES = {
    # Standard editor-layout replay driver.
    "demsky_acid": "MUSICIANS/D/Demsky_Martin/Acid_Bassline.sid",
    "demsky_minimal": "MUSICIANS/D/Demsky_Martin/Minimal.sid",
    "ilkke_glow": "MUSICIANS/I/Ilkke/Glow_Worm.sid",
    "ilkke_klotokok": "MUSICIANS/I/Ilkke/Klotokok.sid",
    "dexd_short": "MUSICIANS/D/Dex-D/Short_Dream.sid",
    # Goto80 compact-runtime replay driver.
    "goto80_rentacop": "MUSICIANS/G/Goto80/Rent-A-Cop.sid",
    # Ponky decodes as the standard ``editor`` layout, not the compact runtime.
    "goto80_ponky": "MUSICIANS/G/Goto80/Ponky.sid",
    # 6581-flagged, filter cutoff driven by osc3/env3 reads.
    "dexd_2manu3l": "MUSICIANS/D/Dex-D/2Manu3L.sid",
    # CIA-timed multi-speed: 2x and 4x calls per frame.
    "fading_lights": "DEMOS/A-F/Fading_Lights.sid",
    "syrapaog": "DEMOS/S-Z/Syrapaog.sid",
}


def _render(data, nframes):
    return oracle_hvsc.render(data, 1, nframes)[0].tolist()


tune_id, oracle_match = make_oracle_fixtures(TUNES, render=_render)


@pytest.mark.oracle
def test_render_matches_oracle(oracle_match):  # noqa: F811
    oracle_match()


# Subtunes past song 1 or diverging past the fixtures' window: Goto80 replays
# whose stray pattern pointers read the BASIC loader's zero-page words.
SUBTUNES = {
    "goto80_reloaded_8": ("MUSICIANS/G/Goto80/Rent-A-Cop_Reloaded.sid", 8),
    "goto80_reloaded_title": ("MUSICIANS/G/Goto80/Rent-A-Cop_Reloaded_title.sid", 1),
}


@pytest.mark.oracle
@pytest.mark.parametrize("key", list(SUBTUNES))
def test_subtune_matches_oracle(key):
    relpath, song = SUBTUNES[key]
    path = resolve_tune(relpath)
    if path is None:
        raise TuneFetchError(f"tune {key} unavailable (offline, not cached)")
    nframes = song_frames(path, song)
    expected = oracle_grid(path, seconds=nframes // 50 + 2, frames=nframes, song=song)
    rendered = oracle_hvsc.render(path.read_bytes(), song, len(expected))[0].tolist()
    assert aligned_match(expected, rendered), f"{key}: render differs from oracle"
