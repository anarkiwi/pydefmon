#!/usr/bin/env python3
"""Find the fewest trial seeds that cover each replay's play routine.

For each tune, run seeds ``0..N``, record which instructions and branch ways the CPU ran
(:mod:`tests._support.routine_coverage`) and choose seeds greedily until nothing more is
shown. Prints the seeds and the features no seed showed, which need a reason each (code
no state reaches) before they go in ``tests/_support/coverage_seeds.json``.
"""

# pylint: disable=wrong-import-position
import argparse
import json
import sys
from pathlib import Path

from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pysidtracker.testing import (
    resolve_tune,
)  # noqa: E402

from pydefmon._spec_layout import (
    entry_base,
)  # noqa: E402
from tests._support.routine_coverage import (  # noqa: E402
    MUTANTS,
    Routine,
    mutated_spec,
)


def search(relpath: str, trials: int) -> dict:
    """The covering seeds of ``relpath`` and the features none of ``trials`` showed."""
    path = resolve_tune(relpath)
    if path is None:
        raise SystemExit(f"tune {relpath} unavailable")
    routine = Routine(path.read_bytes())
    wanted = routine.features()
    shown = {}
    mismatched = []
    for seed in tqdm(range(trials), desc=relpath, leave=False):
        cov, differing = routine.trial(seed)
        if differing:
            mismatched.append((seed, [hex(a) for a in differing[:6]]))
        shown[seed] = routine.shown(cov)
    reachable = set().union(*shown.values())
    chosen, covered = [], set()
    while covered != reachable:
        best = max(shown, key=lambda s: len(shown[s] - covered))
        chosen.append(best)
        covered |= shown[best]
    never = sorted(wanted - reachable)
    killers = {}
    for old, new in MUTANTS:
        mutant = Routine(path.read_bytes(), spec=mutated_spec(old, new))
        for seed in range(min(trials, 400)):
            if mutant.trial(seed)[1]:
                killers[old] = seed
                break
    chosen = chosen + [seed for seed in killers.values() if seed not in chosen]
    base = entry_base(routine.view, routine.entries[0])
    return {
        "seeds": sorted(chosen),
        "killed": sorted(killers),
        "never": [[at - base, flag] for at, flag in never],
        "mismatched": mismatched[:5],
        "entries": [hex(e) for e in routine.entries],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tunes", nargs="+", help="HVSC-relative .sid paths")
    parser.add_argument("--trials", type=int, default=2000)
    args = parser.parse_args()
    out = {rel: search(rel, args.trials) for rel in args.tunes}
    json.dump(out, sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main()
