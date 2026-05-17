"""Recall comparator for ``dump_to_defmon`` round-trips.

Takes two ``defmon_player`` write-log CSVs (frame,reg,value) -- a
*source* CSV rendered from the original .prg and a *recon* CSV
rendered from the .prg that ``dump_to_defmon`` reconstructed from
the source CSV -- and reports overall + per-register-class recall.

The metric is multi-set exact-match of ``(frame, reg, value)``
tuples: a row counts as matched when its triple appears in both
logs with equal or greater multiplicity. Multiplicity matters in
the rare case where a register flips within one frame and back
again (player dedupe is consecutive, not within-frame).

Per-register classes follow the AGENTS.md 2026-05-17 breakdown so
historical numbers stay legible: per-voice freq (lo+hi), CTRL, AD,
SR, PW (lo+hi); globals split into cutoff ($D415+$D416), res
($D417), volume ($D418).

Usage::

    python3 -m profile.dump_to_defmon_recall \\
        SOURCE_CSV RECON_CSV
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

VoiceRegs = {
    0: {"freq": (0, 1), "pw": (2, 3), "ctrl": (4,), "ad": (5,), "sr": (6,)},
    1: {"freq": (7, 8), "pw": (9, 10), "ctrl": (11,), "ad": (12,), "sr": (13,)},
    2: {"freq": (14, 15), "pw": (16, 17), "ctrl": (18,), "ad": (19,), "sr": (20,)},
}
GlobalRegs = {
    "cutoff": (21, 22),
    "res": (23,),
    "volume": (24,),
}


@dataclass(frozen=True)
class ClassResult:
    label: str
    src: int
    matched: int

    @property
    def recall(self) -> float:
        return self.matched / self.src if self.src else 0.0


@dataclass(frozen=True)
class RecallReport:
    src_rows: int
    recon_rows: int
    overall_matched: int
    per_class: tuple[ClassResult, ...]

    @property
    def overall_recall(self) -> float:
        return self.overall_matched / self.src_rows if self.src_rows else 0.0


def load_rows(path: Path) -> Counter:
    """Return Counter[(frame, reg, value)] for a write-log CSV."""
    out: Counter = Counter()
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            out[(int(row["frame"]), int(row["reg"]), int(row["value"]))] += 1
    return out


def _filter_by_regs(counter: Counter, regs: tuple[int, ...]) -> Counter:
    return Counter({k: v for k, v in counter.items() if k[1] in regs})


def _class_intersect(
    src: Counter, recon: Counter, regs: tuple[int, ...]
) -> ClassResult:
    src_class = _filter_by_regs(src, regs)
    recon_class = _filter_by_regs(recon, regs)
    matched = sum((src_class & recon_class).values())
    return ClassResult(
        label="",
        src=sum(src_class.values()),
        matched=matched,
    )


def compute_recall(src_csv: Path, recon_csv: Path) -> RecallReport:
    src = load_rows(src_csv)
    recon = load_rows(recon_csv)

    overall = sum((src & recon).values())

    results: list[ClassResult] = []
    for v, fields in VoiceRegs.items():
        for fname, regs in fields.items():
            r = _class_intersect(src, recon, regs)
            results.append(
                ClassResult(
                    label=f"V{v}.{fname}",
                    src=r.src,
                    matched=r.matched,
                )
            )
    for name, regs in GlobalRegs.items():
        r = _class_intersect(src, recon, regs)
        results.append(
            ClassResult(
                label=name,
                src=r.src,
                matched=r.matched,
            )
        )

    return RecallReport(
        src_rows=sum(src.values()),
        recon_rows=sum(recon.values()),
        overall_matched=overall,
        per_class=tuple(results),
    )


def format_report(rep: RecallReport) -> str:
    lines = []
    lines.append(f"source rows: {rep.src_rows}    recon rows: {rep.recon_rows}")
    lines.append(
        f"overall: {rep.overall_matched}/{rep.src_rows} = "
        f"{rep.overall_recall*100:.2f}%"
    )
    lines.append("")
    lines.append("per register class:")
    for c in rep.per_class:
        lines.append(
            f"  {c.label:10s} {c.matched:6d}/{c.src:6d} = " f"{c.recall*100:6.2f}%"
        )
    return "\n".join(lines)


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source_csv", type=Path)
    ap.add_argument("recon_csv", type=Path)
    args = ap.parse_args(argv)

    rep = compute_recall(args.source_csv, args.recon_csv)
    print(format_report(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
