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
from pydefmon._spec_layout import entry_base

from tests._support.routine_coverage import MUTANTS, Routine, mutated_spec

SEEDS = json.loads(
    (Path(__file__).parent / "_support" / "coverage_seeds.json").read_text()
)
# Replays relocated off ``$1000`` keep absolute operands of that layout (``STA $10CA``,
# ``JMP $174B``) that the CPU follows and the spec does not model, so a random state
# makes them differ; ``Glow_Worm``, ``Rent-A-Cop`` and the two copies of
# ``Timewarp_Spheres_2SID`` are held to the replay by the whole-song tests instead.
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
    for at, flag in never:  # each way is dead: the code that decides it already has
        if flag == FALLTHRU:  # a reader's ``BMI`` after the ``STX`` of a negative X
            assert routine.view[at] == 0x30 and routine.view[at - 3] == 0x8E, hex(at)
        else:  # the row interpreter's ``BEQ`` after the ``LAX`` of a scratch it excluded
            assert flag == TAKEN, hex(at)
            assert routine.view[at] == 0xF0 and routine.view[at - 2] == 0xA7, hex(at)


@pytest.mark.parametrize("old, new", MUTANTS)
def test_a_changed_spec_differs_on_the_states_of_some_tune(old, new):
    mutated = mutated_spec(old, new)
    for relpath in SEEDS:
        path = resolve_tune(relpath)
        if path is None:
            continue
        routine = Routine(path.read_bytes(), spec=mutated)
        if any(routine.trial(seed)[1] for seed in SEEDS[relpath]["seeds"]):
            return
    (
        pytest.skip("no tune available")
        if not any(resolve_tune(rel) for rel in SEEDS)
        else pytest.fail(f"no tune's states tell the spec from {old!r} -> {new!r}")
    )


def test_the_player_is_spec_backed_on_each_tune():
    for relpath in SEEDS:
        path = resolve_tune(relpath)
        if path is not None:
            assert DefmonPlayer(path.read_bytes()).spec_backed, relpath
            assert SidImage.from_bytes(path.read_bytes())
