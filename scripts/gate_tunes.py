"""Write the gate corpus: every tune under an HVSC root that pydefmon's format accepts.

    python scripts/gate_tunes.py HVSC OUT.csv [--jobs J]

The CSV has a ``path`` column of absolute paths, the input of pysidtracker's
``tools/spec_trace.py record|check``.
"""

import argparse
import csv
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from tqdm import tqdm


def accepts(path: str) -> "str | None":
    """``path`` if pydefmon's format parses the tune, else ``None``."""
    # pylint: disable=import-outside-toplevel
    from pysidtracker import find_format

    try:
        data = Path(path).read_bytes()
        return path if find_format(data, "defmon").parser.parse(data) else None
    except Exception:  # pylint: disable=broad-exception-caught
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("hvsc")
    parser.add_argument("out")
    parser.add_argument("--jobs", type=int, default=8)
    args = parser.parse_args()
    paths = sorted(
        os.path.join(root, name)
        for root, _, names in os.walk(args.hvsc)
        for name in names
        if name.lower().endswith(".sid")
    )
    with ProcessPoolExecutor(args.jobs) as pool:
        found = [
            p
            for p in tqdm(pool.map(accepts, paths, chunksize=64), total=len(paths))
            if p
        ]
    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["path"])
        writer.writerows([p] for p in found)
    print(len(found), "of", len(paths))


if __name__ == "__main__":
    main()
