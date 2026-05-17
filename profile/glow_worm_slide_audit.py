"""Audit glow_worm slide prevalence + reverse-mappability.

Walks the canonical glow_worm.csv source, applies the proposed
note-change-only phrase boundary + linear-rate active-slide detection,
and reports the audit numbers that refine the
``dump_to_defmon_slides_design.md`` estimates.

Outputs a Markdown summary to stdout. Per-voice tallies:

* total phrases (with note-change-only boundary)
* # phrases with off-LUT frames
* # phrases fitting a constant-rate active-slide model
* # phrases off-LUT but non-linear (reverse-mapping out of scope)
* slide-start frame T distribution (relative to phrase start)
* slide_mode S distribution (frequency of each fitted slide_mode byte)
* projected sidTAB row growth: # distinct
  ``(WGh, AD, SR, PW, AF, T_offset)`` tuples for the multi-row
  encoding.

Usage::

    python3 -m profile.glow_worm_slide_audit
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from pydefmon.defmon_player import (
    NOTE_PITCH_HI,
    NOTE_PITCH_LO,
    SLIDE_DEC_HI_BASE,
    SLIDE_DEC_LO_BASE,
    SLIDE_INC_HI_BASE,
    SLIDE_INC_LO_BASE,
    _lut,
)

PITCH_BASE_PER_VOICE = (0x00, 0x01, 0x02)


def _build_slide_deltas() -> dict[int, int]:
    """slide_mode (in $80..$FF) → signed 16-bit per-frame freq delta."""
    out: dict[int, int] = {}
    for s in range(0x80, 0x100):
        y = (s << 1) & 0xFF
        if y & 0x80:
            d = -((_lut(SLIDE_DEC_HI_BASE, y) << 8) | _lut(SLIDE_DEC_LO_BASE, y))
        else:
            d = (_lut(SLIDE_INC_HI_BASE, y) << 8) | _lut(SLIDE_INC_LO_BASE, y)
        out[s] = d
    return out


def _build_freq_to_note_lut() -> list[dict[tuple[int, int], int]]:
    """Per-voice (freq_lo, freq_hi) → note, WITH pitch_base offset.
    Matches snap_note's per-voice LUT."""
    out: list[dict[tuple[int, int], int]] = []
    for pb in PITCH_BASE_PER_VOICE:
        m: dict[tuple[int, int], int] = {}
        for note in range(128):
            if NOTE_PITCH_LO[note] == 0 and NOTE_PITCH_HI[note] == 0:
                continue
            total_lo = NOTE_PITCH_LO[note] + pb
            carry = total_lo >> 8
            freq_lo = total_lo & 0xFF
            freq_hi = (NOTE_PITCH_HI[note] + carry) & 0xFF
            m.setdefault((freq_lo, freq_hi), note)
        out.append(m)
    return out


@dataclass
class FrameState:
    freq_lo: list[int] = field(default_factory=lambda: [0, 0, 0])
    freq_hi: list[int] = field(default_factory=lambda: [0, 0, 0])
    ctrl: list[int] = field(default_factory=lambda: [0, 0, 0])
    ad: list[int] = field(default_factory=lambda: [0, 0, 0])
    sr: list[int] = field(default_factory=lambda: [0, 0, 0])
    pw_hi: list[int] = field(default_factory=lambda: [0, 0, 0])
    snapped_note: list[int] = field(default_factory=lambda: [0, 0, 0])


def _replay_csv(csv_path: Path, freq_lut) -> list[FrameState]:
    """Per-frame state replay (including raw freq + snapped_note)."""
    rows_by_frame: dict[int, list[tuple[int, int]]] = {}
    max_frame = -1
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            frame = int(row["frame"])
            reg = int(row["reg"])
            val = int(row["value"])
            rows_by_frame.setdefault(frame, []).append((reg, val))
            max_frame = max(max_frame, frame)

    cur = FrameState()
    states: list[FrameState] = []
    for frame in range(max_frame + 1):
        for reg, val in rows_by_frame.get(frame, []):
            if 0 <= reg <= 20:
                v = reg // 7
                f = reg % 7
                if f == 0:
                    cur.freq_lo[v] = val
                elif f == 1:
                    cur.freq_hi[v] = val
                elif f == 3:
                    cur.pw_hi[v] = val
                elif f == 4:
                    cur.ctrl[v] = val
                elif f == 5:
                    cur.ad[v] = val
                elif f == 6:
                    cur.sr[v] = val
                if f in (0, 1):
                    key = (cur.freq_lo[v], cur.freq_hi[v])
                    if key in freq_lut[v]:
                        cur.snapped_note[v] = freq_lut[v][key]
        states.append(
            FrameState(
                freq_lo=list(cur.freq_lo),
                freq_hi=list(cur.freq_hi),
                ctrl=list(cur.ctrl),
                ad=list(cur.ad),
                sr=list(cur.sr),
                pw_hi=list(cur.pw_hi),
                snapped_note=list(cur.snapped_note),
            )
        )
    return states


@dataclass
class Phrase:
    voice: int
    start_frame: int
    length: int
    note: int
    # Phrase-start column snapshot (the multi-row instrument's row-0 cols).
    wgh: int
    ad: int
    sr: int
    pw_hi: int


def _slice_phrases_note_only(states: list[FrameState], voice: int) -> list[Phrase]:
    """Phrase boundary = note byte change only (no gate-bit toggle).
    Models the proposed slides design's relaxed boundary."""
    phrases: list[Phrase] = []
    cur_start = 0
    cur_note = states[0].snapped_note[voice]
    for f in range(1, len(states)):
        n = states[f].snapped_note[voice]
        if n != cur_note:
            phrases.append(
                Phrase(
                    voice=voice,
                    start_frame=cur_start,
                    length=f - cur_start,
                    note=cur_note,
                    wgh=states[cur_start].ctrl[voice],
                    ad=states[cur_start].ad[voice],
                    sr=states[cur_start].sr[voice],
                    pw_hi=states[cur_start].pw_hi[voice],
                )
            )
            cur_start = f
            cur_note = n
    phrases.append(
        Phrase(
            voice=voice,
            start_frame=cur_start,
            length=len(states) - cur_start,
            note=cur_note,
            wgh=states[cur_start].ctrl[voice],
            ad=states[cur_start].ad[voice],
            sr=states[cur_start].sr[voice],
            pw_hi=states[cur_start].pw_hi[voice],
        )
    )
    return phrases


@dataclass
class SlideHit:
    phrase: Phrase
    t_offset: int  # first frame in phrase where drift != pitch_base
    slide_mode: int  # matched AF byte
    delta: int
    span: int  # number of frames the slide is observed for


@dataclass
class SlideAuditReport:
    voice: int
    total_phrases: int = 0
    no_slide_phrases: int = 0
    slide_hits: list[SlideHit] = field(default_factory=list)
    nonlinear_offlut_phrases: int = 0
    delta_no_match_phrases: int = 0
    t_distribution: Counter = field(default_factory=Counter)
    slide_mode_distribution: Counter = field(default_factory=Counter)
    cluster_keys: set = field(default_factory=set)


def _audit_voice(
    voice: int, states: list[FrameState], slide_deltas: dict[int, int]
) -> SlideAuditReport:
    """Run the slide detection on one voice. Tally outcomes."""
    pb = PITCH_BASE_PER_VOICE[voice]
    # Reverse map delta → slide_mode (multiple S share delta=0, take first).
    delta_to_s: dict[int, int] = {}
    for s, d in slide_deltas.items():
        if d != 0 and d not in delta_to_s:
            delta_to_s[d] = s

    phrases = _slice_phrases_note_only(states, voice)
    rep = SlideAuditReport(voice=voice, total_phrases=len(phrases))

    for p in phrases:
        note = p.note
        base = (NOTE_PITCH_HI[note] << 8) | NOTE_PITCH_LO[note]
        if base == 0:
            # Zero-LUT entry: note 0 (silence). Skip.
            rep.no_slide_phrases += 1
            continue

        drifts = []
        for k in range(p.length):
            fs = states[p.start_frame + k]
            obs = (fs.freq_hi[voice] << 8) | fs.freq_lo[voice]
            d = obs - base
            if d < -0x8000:
                d += 0x10000
            elif d > 0x7FFF:
                d -= 0x10000
            drifts.append(d)

        # Find first frame T where drift != pb (= no-slide expected).
        t_offset = None
        for k, d in enumerate(drifts):
            if d != pb:
                t_offset = k
                break

        if t_offset is None:
            rep.no_slide_phrases += 1
            continue

        # Drift at T is "first observed slide-step". For a linear slide
        # engaged 1 frame before T (slide accumulator = delta at end of
        # frame T-1, captured by emit at frame T).
        # Linearity check: drifts[T+j] - drifts[T+j-1] == delta for all j.
        if t_offset == len(drifts) - 1:
            # Only one off-LUT frame; can't establish linearity.
            rep.nonlinear_offlut_phrases += 1
            continue
        step = drifts[t_offset + 1] - drifts[t_offset]
        # Account for wrap.
        if step < -0x8000:
            step += 0x10000
        elif step > 0x7FFF:
            step -= 0x10000
        if step == 0:
            # No progression — drift held constant. Not a slide.
            rep.nonlinear_offlut_phrases += 1
            continue

        # Verify linearity for all off-LUT frames.
        linear = True
        for j in range(t_offset + 1, len(drifts)):
            prev = drifts[j - 1]
            cur = drifts[j]
            local_step = cur - prev
            if local_step < -0x8000:
                local_step += 0x10000
            elif local_step > 0x7FFF:
                local_step -= 0x10000
            if local_step != step:
                linear = False
                break
        if not linear:
            rep.nonlinear_offlut_phrases += 1
            continue

        # Match step against SLIDE_DELTAS.
        s_match = delta_to_s.get(step)
        if s_match is None:
            rep.delta_no_match_phrases += 1
            continue

        # Hit!
        span = len(drifts) - t_offset
        hit = SlideHit(
            phrase=p, t_offset=t_offset, slide_mode=s_match, delta=step, span=span
        )
        rep.slide_hits.append(hit)
        rep.t_distribution[t_offset] += 1
        rep.slide_mode_distribution[s_match] += 1
        # Cluster key for multi-row instrument estimation:
        #   (WGh, AD, SR, PW) of phrase-start + (AF=s_match, T=t_offset)
        # Matches Instrument.from_phrase's column-set semantics.
        rep.cluster_keys.add((p.wgh, p.ad, p.sr, p.pw_hi, s_match, t_offset))

    return rep


def _format_distribution(c: Counter, top_n: int = 10) -> str:
    items = sorted(c.items(), key=lambda kv: -kv[1])[:top_n]
    return ", ".join(f"{k}:{v}" for k, v in items)


def _format_report(reports: list[SlideAuditReport], total_csv_rows: int) -> str:
    lines = ["# glow_worm slide audit", ""]
    for rep in reports:
        v = rep.voice
        hits = len(rep.slide_hits)
        offlut_total = hits + rep.nonlinear_offlut_phrases + rep.delta_no_match_phrases
        lines.append(f"## V{v}")
        lines.append(
            f"- total phrases (note-change-only boundary): {rep.total_phrases}"
        )
        lines.append(
            f"- no-slide phrases (drift == pitch_base throughout): "
            f"{rep.no_slide_phrases}"
        )
        lines.append(f"- off-LUT phrases (total): {offlut_total}")
        lines.append(f"  - linear active-slide fit + S match: **{hits}**")
        lines.append(
            f"  - off-LUT non-linear (out of scope for MVP): "
            f"{rep.nonlinear_offlut_phrases}"
        )
        lines.append(
            f"  - linear but delta not in SLIDE_DELTAS: "
            f"{rep.delta_no_match_phrases}"
        )
        # Frames captured: count CSV row matches the slide hits would cover.
        slide_frames_covered = sum(h.span * 2 for h in rep.slide_hits)
        lines.append(
            f"- estimated CSV rows recovered (slide-span * 2 regs/frame, "
            f"max): {slide_frames_covered}"
        )
        lines.append(
            f"- T_offset distribution (top 10): "
            f"{_format_distribution(rep.t_distribution)}"
        )
        lines.append(
            f"- slide_mode S distribution (top 10): "
            f"{_format_distribution({hex(k): v for k, v in rep.slide_mode_distribution.items()})}"
        )
        lines.append(
            f"- distinct (WGh,AD,SR,PW,AF,T) cluster keys: " f"{len(rep.cluster_keys)}"
        )
        lines.append("")

    total_hits = sum(len(r.slide_hits) for r in reports)
    total_frames_covered = sum(sum(h.span * 2 for h in r.slide_hits) for r in reports)
    total_cluster_keys = sum(len(r.cluster_keys) for r in reports)
    lines.append("## Summary")
    lines.append(f"- total CSV rows (source): {total_csv_rows}")
    lines.append(f"- slide hits across voices: {total_hits}")
    lines.append(f"- max CSV rows recovered (slide-span * 2): {total_frames_covered}")
    lines.append(
        f"- distinct multi-row cluster keys across voices: {total_cluster_keys}"
    )
    lines.append(
        "  (= upper bound on extra sidTAB rows for slide encoding; "
        "cross-voice merge in build_song dedupes these)"
    )

    return "\n".join(lines)


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "csv",
        type=Path,
        nargs="?",
        default=Path("fixtures/glow_worm.csv"),
    )
    args = ap.parse_args(argv)

    slide_deltas = _build_slide_deltas()
    freq_lut = _build_freq_to_note_lut()
    states = _replay_csv(args.csv, freq_lut)

    # Count total CSV rows for context.
    with open(args.csv) as f:
        total_rows = sum(1 for _ in csv.DictReader(f))

    reports = [_audit_voice(v, states, slide_deltas) for v in range(3)]
    print(_format_report(reports, total_rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
