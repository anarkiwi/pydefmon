"""The ``scripts/oracle_hvsc.py`` survey renderer and its render contract."""

import json
import re
import sys
from pathlib import Path

import numpy as np
import pytest

from pysidtracker import SidParseError
from pysidtracker.sidid import sidid_signatures

from pydefmon import DefmonPlayer

from tests._support import resolve_tune, synthetic_replay

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import oracle_hvsc  # noqa: E402  (after sys.path tweak)


def _tune(relpath):
    path = resolve_tune(relpath)
    if path is None:
        pytest.skip(f"{relpath} unreachable (offline)")
    return path.read_bytes()


@pytest.mark.parametrize(
    "relpath, layout",
    [
        ("MUSICIANS/I/Ilkke/Glow_Worm.sid", "editor"),
        ("MUSICIANS/G/Goto80/Rent-A-Cop.sid", "goto80"),
    ],
)
def test_render_matches_player_and_reports_variant(relpath, layout):
    data = _tune(relpath)
    grid, info = oracle_hvsc.render(data, 1, 60)
    assert grid.shape == (60, 25)
    np.testing.assert_array_equal(grid, DefmonPlayer(data, subtune=0).render_grid(60))
    assert json.loads(json.dumps(info)) == info
    assert info == {
        "layout": layout,
        "rsid": False,
        "trigger": "pal_video",
        "speed": 1,
        "dynamic": False,
    }


def test_render_selects_one_based_subtune():
    data = _tune("MUSICIANS/G/Goto80/Rent-A-Cop.sid")
    second = oracle_hvsc.render(data, 2, 100)[0]
    np.testing.assert_array_equal(
        second, DefmonPlayer(data, subtune=1).render_grid(100)
    )
    assert (second != oracle_hvsc.render(data, 1, 100)[0]).any()


@pytest.mark.parametrize("data", [synthetic_replay(), b"\x00\x18junk", b"PSID"])
def test_non_defmon_raises_sid_parse_error(data):
    with pytest.raises(SidParseError):
        oracle_hvsc.render(data, 1, 10)


def test_player_regex_selects_defmon_signature():
    names = [name for name, _ in sidid_signatures()]
    assert [n for n in names if re.search(oracle_hvsc.PLAYER, n, re.I)] == ["DefMon"]
