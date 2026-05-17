"""Audit a defmon_player CSV write log against the player's emit model.

For each voice, reconstruct (freq_lo, freq_hi) over time and check it
maps to a (note, pitch_base_offset) pair from the embedded note LUT.
For each voice's (pw_lo, pw_hi), check the 12-bit constraint
(pw_lo == pw_hi & 0xF0). For global writes, check that
$D418 has low nibble $0F (FV emit constraint) and that $D417 values
have bits 7 and 3 clear (RE column direct-write constraint).

Also count "phrases" per voice -- transitions in (note byte, gate bit).

Usage: ``python3 -m profile.audit_csv_reachability CSV``
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

from pydefmon.defmon_player import NOTE_PITCH_HI, NOTE_PITCH_LO

PITCH_BASE = (0x00, 0x01, 0x02)  # V0, V1, V2 hardcoded in defMON static binary


def _build_freq_to_note_per_voice() -> list[dict[tuple[int, int], int]]:
    """Build reverse map per voice: (freq_lo, freq_hi) -> note byte."""
    out: list[dict[tuple[int, int], int]] = []
    for v_idx, pb in enumerate(PITCH_BASE):
        m: dict[tuple[int, int], int] = {}
        for note in range(128):
            total_lo = NOTE_PITCH_LO[note] + pb
            carry = total_lo >> 8
            freq_lo = total_lo & 0xFF
            freq_hi = (NOTE_PITCH_HI[note] + carry) & 0xFF
            # Skip the zero entries that pad the LUT past note 119.
            if NOTE_PITCH_LO[note] == 0 and NOTE_PITCH_HI[note] == 0:
                continue
            m.setdefault((freq_lo, freq_hi), note)
        out.append(m)
    return out


VOICE_REG_RANGES = {
    0: range(0, 7),   # V0: $D400..$D406
    1: range(7, 14),  # V1: $D407..$D40D
    2: range(14, 21),  # V2: $D40E..$D414
}


def voice_for_reg(reg: int) -> int | None:
    for v, rng in VOICE_REG_RANGES.items():
        if reg in rng:
            return v
    return None


def reg_field(reg: int) -> str:
    """V?: freq_lo / freq_hi / pw_lo / pw_hi / ctrl / ad / sr; or global name."""
    v = voice_for_reg(reg)
    if v is None:
        return {21: "filter_cutoff_lo", 22: "filter_cutoff_hi",
                23: "filter_res", 24: "volume_mode"}.get(reg, f"reg{reg}")
    field = ("freq_lo", "freq_hi", "pw_lo", "pw_hi", "ctrl", "ad", "sr")[
        reg - 7 * v
    ]
    return f"V{v}.{field}"


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv", type=Path)
    args = ap.parse_args(argv)

    freq_maps = _build_freq_to_note_per_voice()

    state = [0] * 25
    voice_notes = [None, None, None]  # last note byte chosen for each voice
    phrases = [0, 0, 0]  # phrase boundary count per voice
    last_phrase_signature = [(None, None), (None, None), (None, None)]
    gate_state = [0, 0, 0]  # last gate bit per voice
    pw_unreachable = Counter()  # (voice -> count)
    freq_unreachable = Counter()  # (voice -> count)
    freq_unreachable_examples: dict[int, list[tuple[int, int]]] = {0: [], 1: [], 2: []}
    fv_unreachable = 0
    re_unreachable = 0
    total_rows = 0
    per_voice_changes = Counter()
    per_global_changes = Counter()

    with open(args.csv) as f:
        reader = csv.DictReader(f)
        for row in reader:
            total_rows += 1
            frame = int(row["frame"])
            reg = int(row["reg"])
            val = int(row["value"])
            old = state[reg]
            state[reg] = val
            v = voice_for_reg(reg)
            field = reg_field(reg)

            if v is not None:
                per_voice_changes[v] += 1
                rel = reg - 7 * v
                if rel in (0, 1):  # freq change
                    fl = state[7 * v + 0]
                    fh = state[7 * v + 1]
                    note = freq_maps[v].get((fl, fh))
                    if note is None:
                        freq_unreachable[v] += 1
                        if len(freq_unreachable_examples[v]) < 5:
                            freq_unreachable_examples[v].append((fl, fh))
                    else:
                        if note != voice_notes[v]:
                            phrases[v] += 1
                            voice_notes[v] = note
                elif rel in (2, 3):  # pw change
                    pl = state[7 * v + 2]
                    ph = state[7 * v + 3]
                    if pl != (ph & 0xF0):
                        pw_unreachable[v] += 1
                elif rel == 4:  # ctrl
                    new_gate = val & 0x01
                    if new_gate != gate_state[v]:
                        phrases[v] += 1
                        gate_state[v] = new_gate
            else:
                per_global_changes[reg] += 1
                if reg == 24:  # FV / volume
                    if (val & 0x0F) != 0x0F:
                        fv_unreachable += 1
                elif reg == 23:  # RE / res
                    if (val & 0x88) != 0:
                        re_unreachable += 1

    print(f"== {args.csv} ==")
    print(f"total rows: {total_rows}")
    print()
    print("per-voice changes:")
    for v in (0, 1, 2):
        print(f"  V{v}: {per_voice_changes[v]} CSV rows, {phrases[v]} phrase boundaries")
    print()
    print("per-global changes:")
    for reg in sorted(per_global_changes):
        print(f"  {reg_field(reg)} (reg {reg}): {per_global_changes[reg]} rows")
    print()
    print("reachability failures:")
    print(f"  freq off-LUT: V0={freq_unreachable[0]} V1={freq_unreachable[1]} V2={freq_unreachable[2]}")
    for v in (0, 1, 2):
        if freq_unreachable_examples[v]:
            ex = ", ".join(f"(${l:02X},${h:02X})" for l, h in freq_unreachable_examples[v])
            print(f"    V{v} examples: {ex}")
    print(f"  pw 12-bit-broken: V0={pw_unreachable[0]} V1={pw_unreachable[1]} V2={pw_unreachable[2]}")
    print(f"  FV (low nibble != $F): {fv_unreachable}")
    print(f"  RE (bits 7|3 set): {re_unreachable}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
