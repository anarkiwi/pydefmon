"""The play routine's code against ``defmon.play``, by what the code does.

For each replay a handful of random states (:mod:`tests._support.routine_coverage`) are
run on the CPU and as the spec, and the whole 64 KiB outside the stack page and the SID
must agree. The states are the fewest whose runs between them execute every instruction
the play entries reach and take each branch both ways, bar the ways no state reaches
(``never``, each with its reason); a change that moves the coverage, or a spec that
differs on any of the states, fails.
"""

import json
from pathlib import Path

import pytest

from jennings.coverage._core import EXEC, FALLTHRU, TAKEN
from pysidtracker import SidImage
from pysidtracker.playspec import parse
from pysidtracker.testing import resolve_tune

from pydefmon import DefmonPlayer
from pydefmon._spec_layout import entry_base, spec_text

from tests._support.routine_coverage import Routine

SEEDS = json.loads(
    (Path(__file__).parent / "_support" / "coverage_seeds.json").read_text()
)
FLAGS = {"exec": EXEC, "taken": TAKEN, "fallthru": FALLTHRU}


def _routine(relpath: str) -> Routine:
    path = resolve_tune(relpath)
    if path is None:
        pytest.skip(f"tune {relpath} unavailable (offline, not cached)")
    return Routine(path.read_bytes())


@pytest.mark.parametrize("relpath", sorted(SEEDS))
def test_the_spec_equals_the_code_on_states_that_cover_it(relpath):
    routine = _routine(relpath)
    base = entry_base(routine.view, routine.entries[0])
    never = {(base + at, FLAGS[flag]) for at, flag in SEEDS[relpath]["never"]}
    shown = set()
    for seed in SEEDS[relpath]["seeds"]:
        cov, differing = routine.trial(seed)
        assert not differing, (seed, [hex(at) for at in differing[:8]])
        shown |= routine.shown(cov)
    assert routine.features() - shown == never


def _mutations():
    """The spec with one operator or constant changed, each of which the states must catch."""
    text = spec_text("defmon")
    for old, new in (
        ("c := carry(cut_lo, cut_step_lo);", "c := false;"),
        ("if neg(out) || out < bottom {", "if neg(out) {"),
        ("count := v3[y];", "count := v3[y] + 1;"),
        ("ptr := ptr + u16(y);", "ptr := ptr + u16(y) + 1;"),
        ("counter := fl & 0x0F;", "counter := fl & 0x07;"),
        ("blk_rowidx := eff + 1;", "blk_rowidx := eff;"),
        ("if rows_hi[eff] == 0 {", "if rows_hi[eff] == 1 {"),
        ("res := res & mem[L.vmask + v];", "res := res;"),
    ):
        assert old in text, old
        yield old, text.replace(old, new, 1)


@pytest.mark.parametrize("old, mutated", list(_mutations()))
def test_a_changed_spec_differs_on_those_states(old, mutated):
    relpath = "MUSICIANS/D/Demsky_Martin/Acid_Bassline.sid"
    path = resolve_tune(relpath)
    if path is None:
        pytest.skip(f"tune {relpath} unavailable (offline, not cached)")
    routine = Routine(path.read_bytes(), spec=parse(mutated))
    assert any(routine.trial(seed)[1] for seed in SEEDS[relpath]["seeds"]), old


def test_the_player_is_spec_backed_on_each_tune():
    for relpath in SEEDS:
        path = resolve_tune(relpath)
        if path is not None:
            assert DefmonPlayer(path.read_bytes()).spec_backed, relpath
            assert SidImage.from_bytes(path.read_bytes())
