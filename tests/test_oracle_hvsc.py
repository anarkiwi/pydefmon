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
families and multiple authors, all single-speed and byte-exact under jennings.
"""

import os
from pathlib import Path

import pytest

from pysidtracker import make_oracle_fixtures

from pydefmon import DefmonPlayer

# Cache under the workspace (a Docker-daemon-visible path, and what CI persists
# via actions/cache). ``$PYDEFMON_ORACLE_CACHE`` overrides the location.
_CACHE = Path(os.environ.get("PYDEFMON_ORACLE_CACHE", ".oracle-cache"))

# HVSC defMON ``.sid`` replays verified byte-exact against the deterministic
# oracle. Covers both runtime replay variations pydefmon supports plus a spread
# of authors:
#   * standard defMON editor-layout replay -- Demsky, Ilkke, Dex-D;
#   * Goto80's older compact-runtime replay -- Rent-A-Cop, Ponky.
# (Multi-speed and V3-oscillator-RNG tunes are byte-exact only under a
# cycle-accurate VICE, not the jennings model, so they are excluded here.)
TUNES = {
    # Standard editor-layout replay driver.
    "demsky_acid": "MUSICIANS/D/Demsky_Martin/Acid_Bassline.sid",
    "demsky_minimal": "MUSICIANS/D/Demsky_Martin/Minimal.sid",
    "ilkke_glow": "MUSICIANS/I/Ilkke/Glow_Worm.sid",
    "ilkke_klotokok": "MUSICIANS/I/Ilkke/Klotokok.sid",
    "dexd_short": "MUSICIANS/D/Dex-D/Short_Dream.sid",
    # Goto80 compact-runtime replay driver.
    "goto80_rentacop": "MUSICIANS/G/Goto80/Rent-A-Cop.sid",
    "goto80_ponky": "MUSICIANS/G/Goto80/Ponky.sid",
}


def _render(data, nframes):
    return DefmonPlayer(data).render_grid(nframes)


tune_id, oracle_match = make_oracle_fixtures(
    TUNES,
    hvsc_cache=_CACHE / "hvsc",
    oracle_cache=_CACHE / "csv",
    render=_render,
    frames=250,
)


@pytest.mark.oracle
def test_render_matches_oracle(oracle_match):  # noqa: F811
    oracle_match()
