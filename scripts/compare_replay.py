#!/usr/bin/env python3
"""Compare the spec-backed player with the replay's own 6502, play call by play call.

    python scripts/compare_replay.py TUNES.csv [--frames N] [--jobs J]

``TUNES.csv`` has a ``path`` column (``scripts/gate_tunes.py``). Every play call is run on
both, including those the real interrupt would miss, and what the chip can hear (the
registers without their unused bits) must agree. Prints each tune that does not, or has no
spec, with the first call that differs; exits with how many differ.
"""

# pylint: disable=wrong-import-position
import argparse
import csv
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from tqdm import tqdm

sys.path.append(str(Path(__file__).resolve().parent.parent))

from tests._support import every_call_player  # noqa: E402

#: The bits of each SID register the chip ignores.
UNUSED = {3: 0x0F, 10: 0x0F, 17: 0x0F, 21: 0x07}


def audible(registers) -> tuple:
    """The 25 ``registers`` with the bits the chip ignores cleared."""
    return tuple(r & UNUSED.get(i, 0xFF) for i, r in enumerate(registers))


def compare(job) -> tuple:
    """``(path, None)`` if the spec and the 6502 agree, else why not."""
    path, frames = job
    # pylint: disable=import-outside-toplevel
    from pydefmon import DefmonPlayer

    try:
        data = Path(path).read_bytes()
        spec, plain = DefmonPlayer(data), every_call_player(data)
        if not spec.spec_backed:
            return path, "no spec"
        for call in range(frames):
            plain.play_frame()
            spec.play_frame()
            if audible(plain.snapshot()) != audible(spec.snapshot()):
                return path, f"call {call}"
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return path, f"{type(exc).__name__}: {str(exc)[:80]}"
    return path, None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("tunes")
    parser.add_argument("--frames", type=int, default=3000)
    parser.add_argument("--jobs", type=int, default=8)
    args = parser.parse_args()
    with open(args.tunes, encoding="utf-8") as handle:
        jobs = [(row["path"], args.frames) for row in csv.DictReader(handle)]
    with ProcessPoolExecutor(args.jobs) as pool:
        results = list(tqdm(pool.map(compare, jobs), total=len(jobs), unit="tune"))
    bad = [(path, why) for path, why in results if why]
    for path, why in bad:
        print(Path(path).name, why)
    print("differ", len(bad), "of", len(results))
    return len(bad)


if __name__ == "__main__":
    sys.exit(main())
