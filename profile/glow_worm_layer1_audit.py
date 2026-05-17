"""Audit glow_worm layer-1 cascade prevalence + reverse-mappability.

Walks the canonical glow_worm.csv source, profiles the global
register streams that layer-1 drives ($D416 cutoff, $D417 RE,
$D418 FV, $D415 cutoff-lo), segments the cutoff trajectory into
"epochs" of constant-step slide (with ADC/SBC direction and step
magnitude), counts reset points where an absolute ACID set fires,
and projects the sidTAB row count + GATE_B event count the
``dump_to_defmon`` layer-1 planner would emit.

Outputs a Markdown summary to stdout — the numbers the
``dump_to_defmon_layer1_design.md`` doc cites as
"glow_worm-derived expected lift".

Usage::

    python3 -m profile.glow_worm_layer1_audit
        [path/to/glow_worm.csv] [path/to/glow_worm.prg]

When both fixture paths are omitted, defaults to the project's
``fixtures/`` layout.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "fixtures"
DEFAULT_CSV = FIXTURE_DIR / "glow_worm.csv"
DEFAULT_PRG = FIXTURE_DIR / "glow_worm.prg"

# SID register addresses for the global-state regs.
D415, D416, D417, D418 = 0x15, 0x16, 0x17, 0x18


def _read_csv_per_frame(csv_path: Path) -> list[dict[int, int]]:
    """Read the CSV write-log; return ``frames[F][reg] = last_value``
    for each frame F in [0, max_frame]. Registers that didn't change
    in frame F are absent."""
    by_frame: dict[int, dict[int, int]] = {}
    max_f = -1
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            fr = int(row["frame"])
            reg = int(row["reg"])
            val = int(row["value"])
            by_frame.setdefault(fr, {})[reg] = val
            max_f = max(max_f, fr)
    return [by_frame.get(f, {}) for f in range(max_f + 1)]


def _cumulative_d416(frames: list[dict[int, int]]) -> list[int]:
    """Carry $D416 forward: the emit at frame F is the last write
    at or before F, defaulting to $02 (player init = floor) at the
    start."""
    cur = 0x02
    out: list[int] = []
    for f in frames:
        if D416 in f:
            cur = f[D416]
        out.append(cur)
    return out


def _segment_runs(traj: list[int]) -> list[tuple[int, int, int]]:
    """Run-length-encode the per-frame delta sequence. Each run is
    ``(start_frame, length, delta)``. The first emit (frame 0)
    seeds the chain at delta=0."""
    if len(traj) < 2:
        return []
    runs: list[tuple[int, int, int]] = []
    cur_delta = traj[1] - traj[0]
    cur_start = 1
    cur_len = 1
    for f in range(2, len(traj)):
        d = traj[f] - traj[f - 1]
        if d == cur_delta:
            cur_len += 1
        else:
            runs.append((cur_start, cur_len, cur_delta))
            cur_start = f
            cur_delta = d
            cur_len = 1
    runs.append((cur_start, cur_len, cur_delta))
    return runs


def _find_reset_frames(traj: list[int], peak_value: int) -> list[int]:
    """Frames where the trajectory hits ``peak_value`` (the absolute-
    set "$40 = 64" reset signature)."""
    return [f for f, v in enumerate(traj) if v == peak_value]


def _re_writes(frames: list[dict[int, int]]) -> list[tuple[int, int]]:
    """Per-frame $D417 (RE) writes — only the frames where the
    register actually changed."""
    out: list[tuple[int, int]] = []
    prev = None
    for fr, f in enumerate(frames):
        if D417 in f:
            v = f[D417]
            if prev is None or v != prev:
                out.append((fr, v))
                prev = v
    return out


def _slot_b_audit(prg_path: Path) -> dict:
    """Inspect the source .prg's pattern bodies + arranger to count
    GATE_B events per voice and per layer-1 row target."""
    sys.path.insert(0, str(REPO_ROOT))
    from pydefmon.defmon import DefmonSong

    song = DefmonSong.from_file(str(prg_path))
    voice_to_patterns = {v: set() for v in range(3)}
    for v_idx, arranger in enumerate(
        (song.arranger_v1, song.arranger_v2, song.arranger_v3)
    ):
        for p in arranger:
            if 0 < p < 128:
                voice_to_patterns[v_idx].add(p)

    per_voice: dict[int, dict] = {}
    for v_idx in range(3):
        targets: Counter = Counter()
        for pat in voice_to_patterns[v_idx]:
            for e in song.pattern_events(pat):
                if e.flag & 0x20:  # FLAG_GATE_B
                    targets[e.slot_b] += 1
        per_voice[v_idx] = {
            "n_patterns": len(voice_to_patterns[v_idx]),
            "n_gate_b": sum(targets.values()),
            "targets": dict(sorted(targets.items())),
        }
    return per_voice


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv", nargs="?", default=str(DEFAULT_CSV))
    ap.add_argument("prg", nargs="?", default=str(DEFAULT_PRG))
    args = ap.parse_args(argv)

    csv_path = Path(args.csv)
    prg_path = Path(args.prg)

    frames = _read_csv_per_frame(csv_path)
    n_frames = len(frames)
    cutoff_traj = _cumulative_d416(frames)
    runs = _segment_runs(cutoff_traj)
    re_changes = _re_writes(frames)

    # Detect "absolute reset" frames: where cutoff equals 64 (=$40)
    # AND it differs from the prior frame by > 1 (= jump-to-peak,
    # not a natural slide step).
    reset_frames: list[int] = []
    for f in range(1, n_frames):
        if cutoff_traj[f] == 0x40 and abs(cutoff_traj[f] - cutoff_traj[f - 1]) > 1:
            reset_frames.append(f)

    # Run-length stats: collect signed deltas + their frequencies.
    delta_hist: Counter = Counter()
    for _, length, delta in runs:
        delta_hist[delta] += length

    # Slot_b audit from the .prg.
    slot_b = _slot_b_audit(prg_path)

    print(f"# glow_worm layer-1 audit")
    print()
    print(f"source: `{csv_path.relative_to(REPO_ROOT)}` " f"({n_frames} frames)")
    print(f"source .prg: `{prg_path.relative_to(REPO_ROOT)}`")
    print()
    print("## $D416 (cutoff hi) trajectory")
    print()
    print(f"- emit-value range: min={min(cutoff_traj)} max={max(cutoff_traj)}")
    print(f"- distinct values: {len(set(cutoff_traj))}")
    print(f"- total run-length segments: {len(runs)}")
    print()
    print(
        "### Top-10 per-frame deltas (signed; weight = total frames "
        "in matching runs)"
    )
    print()
    print("| delta | frames | %  |")
    print("|---:|---:|---:|")
    for delta, count in delta_hist.most_common(10):
        pct = 100.0 * count / max(1, sum(delta_hist.values()))
        print(f"| {delta:+d} | {count} | {pct:.1f}% |")
    print()
    print("### Detected ACID reset frames (cutoff jumps to $40)")
    print()
    print(f"- count: {len(reset_frames)}")
    print(f"- first 10: {reset_frames[:10]}")
    if reset_frames:
        gaps = [
            reset_frames[i + 1] - reset_frames[i] for i in range(len(reset_frames) - 1)
        ]
        print(
            f"- inter-reset gap min/median/max: "
            f"{min(gaps)}/{sorted(gaps)[len(gaps)//2]}/{max(gaps)}"
        )
    print()
    print("## $D417 (RE) write stream")
    print()
    print(f"- distinct write events: {len(re_changes)}")
    if re_changes:
        re_vals: Counter = Counter(v for _, v in re_changes)
        print(f"- distinct values: {dict(sorted(re_vals.items()))}")
        print(f"- first 5: {re_changes[:5]}")
    print()
    print("## .prg GATE_B distribution (slot_b targets in patterns)")
    print()
    print("| voice | patterns | GATE_B events | slot_b targets |")
    print("|---:|---:|---:|---|")
    for v_idx in range(3):
        sb = slot_b[v_idx]
        print(
            f"| V{v_idx} | {sb['n_patterns']} | {sb['n_gate_b']} | "
            f"{sb['targets']} |"
        )
    print()
    print("## Planner projection")
    print()
    print(
        f"- expected GATE_B emissions: at least {len(reset_frames)} "
        f"(one per ACID-reset frame)"
    )
    print(
        f"- expected RE column applications: at least "
        f"{len(re_changes)} (one per RE change)"
    )
    print(
        "- expected layer-1 sidTAB rows: ~6 " "(ACID abs/SBC/ADC + RE/FV/CP set rows)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
