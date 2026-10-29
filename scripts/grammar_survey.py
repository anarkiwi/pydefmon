"""Decode every cached defMON tune into the song grammar and report what fails.

Usage: python scripts/grammar_survey.py [paths...] (default: the tune cache).
"""

import argparse
import glob
import os
import sys
from collections import Counter

from tqdm import tqdm

from pydefmon import DefmonSong
from pydefmon.tune import Tune


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args()
    cache = os.environ.get("PYSID_TUNECACHE", ".tunecache")
    paths = args.paths or sorted(
        glob.glob(f"{cache}/**/*.sid", recursive=True)
        + glob.glob(f"{cache}/disks/defmon-withtunes/*.prg")
    )
    errors: Counter = Counter()
    for path in tqdm(paths, unit="tune"):
        try:
            tune = Tune.from_song(DefmonSong.from_file(path))
            assert Tune.parse(str(tune)) == tune, "text round trip"
            canon = tune.canonical()
            assert canon.canonical() == canon, "canonical idempotence"
        except Exception as exc:  # pylint: disable=broad-except
            errors[f"{type(exc).__name__}: {exc}"] += 1
            tqdm.write(f"{os.path.basename(path)}: {type(exc).__name__}: {exc}")
    print(f"{len(paths) - sum(errors.values())}/{len(paths)} decoded")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
