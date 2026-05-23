"""Integration test: pydefmon's DefmonPlayer vs the real defMON player IRQ.

Boots defMON inside an asid-vice container (driven via ``defmon-driver``),
loads ``.GLOW WORM``, presses F1, snapshots the live player's runtime
state via the binary monitor, then captures every SID register write
the real defMON player emits via VICE's ``-sounddev dump`` mode
(``<clks> <irq_clks> <nmi_clks> <chipno> <addr> <byte>`` per write —
see ``asid-vice/src/arch/shared/sounddrv/sounddump.c``).

The captured runtime state is injected into pydefmon's
``DefmonPlayer.import_runtime_state``, which aligns pydefmon with
the live binary's editor-leftover operand slots (otherwise pydefmon
starts from a fresh ``player_init`` state that doesn't match the
binary's post-F1 state, since defMON's player_init also runs on every
UI mode change — the operand slots accumulate whatever the editor's
audio-preview engine wrote between boot and F1).

Records are batched into per-NMI frames by gap-detecting clks deltas
(within-frame writes are < ~500 cycles apart; between-frame deltas
are ~23400 for the .GLOW WORM tune's CIA-2 timer rate). For each
frame we reduce to ``{reg: last_value}``. In parallel pydefmon's
``play_frame()`` is run for the same frame count and compared
register-by-register.

Requires:
    - docker on PATH
    - ``defmon-driver`` 0.3.0 + ``vice-driver`` 0.2.0
      (``pip install pydefmon[integration]``)
    - ``anarkiwi/headlessvice:latest`` docker image (pulled on demand)
    - ``defmon-withtunes.d64`` + ``glow_worm.prg`` cached under
      ``$PYDEFMON_FIXTURES_DIR`` (``python -m tools.fetch_fixtures --keep-d64``)
    - env var ``PYDEFMON_INTEGRATION=1`` (opt-in)

The headlessvice image has no ``ENTRYPOINT`` (CMD is ``/bin/bash``) and
its ``x64sc`` segfaults unless ``$HOME/.local/state/vice/`` exists for
the log file. We override the entrypoint and bind-mount a writable
state dir to satisfy both.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from pydefmon.defmon import DefmonSong
from pydefmon.defmon_player import DefmonPlayer, SID_REG_BASE

from tests._support import FIXTURES_DIR

log = logging.getLogger(__name__)

HEADLESSVICE_IMAGE = "anarkiwi/headlessvice:latest"
BINMON_PORT = int(os.environ.get("PYDEFMON_BINMON_PORT", "6502"))
WAIT_BOOT_TIMEOUT_S = 90.0
TUNE_NAME = ".GLOW WORM"
TUNE_PRG = "glow_worm.prg"
D64_NAME = "defmon-withtunes.d64"
CONTAINER_NAME = f"pydefmon-player-{os.getpid()}"
# Real-time seconds to let defMON play under -warp.
PLAY_DURATION_S = 30.0
# Number of main frames to assert match after the snapshot point.
COMPARE_FRAMES = 300
# Cycle delta that signals the start of a new player NMI frame.
FRAME_GAP_CYCLES = 5000

# Memory window snapshotted from VICE to seed pydefmon's player state.
# Covers the player IRQ band ($1019..$14EE: per-voice records, patch
# operands, row timers, cascade state, current_note, filter globals,
# silence flag, arranger index, JP-arm slots) AND the snapshot region
# ($1800..$7166: song-position arrays, pat_base ptr cache, arrangers,
# sidTAB DL bytes, pattern bodies, sidTAB rows). The snapshot region
# is needed because pydefmon's cascade reads JP markers / runtime
# pointers from $1800/$1900 and DL bytes from $1E00 directly from
# self.snapshot, and the live binary's editor-runtime activity can
# leave those bytes in a state pydefmon's unpacked_snapshot() doesn't
# reproduce.
STATE_SNAPSHOT_START = 0x1019
STATE_SNAPSHOT_END = 0x7166  # inclusive

# Player-IRQ entry — first instruction of $1022 player_play_body's SID
# write band. Halting here puts the snapshot at a deterministic
# pre-NMI boundary: all per-voice operand slots and global filter
# state are in their post-prior-NMI-finalised form, and the very next
# instruction the CPU executes after resume is the first SID-write
# operand load — i.e. real defMON's "first post-snapshot $D416 emit"
# will be the result of the slide step that consumes exactly the
# acc_hi we just imported. Eliminates the off-by-one we saw with
# arbitrary-instruction-boundary halts via mem_get alone.
PLAYER_ENTRY_PC = 0x1022

# Post-halt sentinel: at the run_until_pc halt we bm.mem_set $D415
# (= reg $15, filter_cutoff_lo — defMON's player only ever writes
# this reg once, at boot, and never reads it) to a magic value. With
# side_effects=True, VICE routes the mem_set through the SID-store
# callback, so the sentinel write lands in the dump file alongside
# the CPU's writes. We then walk the dump to find the sentinel
# record and treat everything AFTER it as the post-halt window —
# eliminating the libc-fully-buffered fopen("w") lag that the
# pre-sentinel approach (using dump_path.stat().st_size at halt) had.
SENTINEL_REG = 0x15  # $D415 filter_cutoff_lo
SENTINEL_VALUE = 0xA5


def _skip_reason() -> str | None:
    if os.environ.get("PYDEFMON_INTEGRATION") != "1":
        return "set PYDEFMON_INTEGRATION=1 to run live-VICE integration tests"
    if shutil.which("docker") is None:
        return "docker not on PATH"
    try:
        import defmon_driver  # noqa: F401
        import vice_driver  # noqa: F401
    except ImportError as e:
        return f"defmon-driver / vice-driver not installed ({e})"
    if not (FIXTURES_DIR / D64_NAME).exists():
        return (
            f"missing fixture {FIXTURES_DIR / D64_NAME}; "
            "run `python -m tools.fetch_fixtures --keep-d64`"
        )
    if not (FIXTURES_DIR / TUNE_PRG).exists():
        return (
            f"missing fixture {FIXTURES_DIR / TUNE_PRG}; "
            "run `python -m tools.fetch_fixtures`"
        )
    return None


def _start_headlessvice(workdir: Path, dump_path: Path) -> str:
    """Boot anarkiwi/headlessvice with x64sc entrypoint, sound dump
    enabled, autostarted on the d64."""
    log_dir = workdir / "vice_state"
    log_dir.mkdir()
    dump_path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        CONTAINER_NAME,
        "-p",
        f"{BINMON_PORT}:6502",
        "-e",
        "HOME=/tmp",
        "-v",
        f"{log_dir}:/tmp/.local/state/vice:rw",
        "-v",
        f"{dump_path.parent}:/dump:rw",
        "-v",
        f"{workdir / 'disk.d64'}:/work/disk.d64:rw",
        "--entrypoint",
        "x64sc",
        HEADLESSVICE_IMAGE,
        "-default",
        "-binarymonitor",
        "-binarymonitoraddress",
        "ip4://0.0.0.0:6502",
        # sounddev=dump auto-uses the dump2 6-column format.
        "-sounddev",
        "dump",
        "-soundarg",
        f"/dump/{dump_path.name}",
        "-warp",
        "-autostart",
        "/work/disk.d64",
    ]
    return subprocess.check_output(args, text=True).strip()


def _stop_container(cid: str) -> None:
    subprocess.run(
        ["docker", "stop", "-t", "2", cid],
        check=False,
        capture_output=True,
    )


def _parse_dump(path: Path) -> list[tuple[int, int, int, int, int, int]]:
    """Parse a VICE dump2 file into 6-tuples per record."""
    out = []
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) != 6:
            continue
        out.append(tuple(int(p) for p in parts))  # type: ignore[arg-type]
    return out


def _batch_into_frames(
    records: list[tuple[int, int, int, int, int, int]],
    *,
    gap_cycles: int = FRAME_GAP_CYCLES,
) -> list[list[tuple[int, int, int, int, int, int]]]:
    """Group consecutive records into per-NMI frame batches."""
    frames: list[list[tuple[int, int, int, int, int, int]]] = []
    current: list[tuple[int, int, int, int, int, int]] = []
    for r in records:
        clks_delta = r[0]
        if current and clks_delta > gap_cycles:
            frames.append(current)
            current = []
        current.append(r)
    if current:
        frames.append(current)
    return frames


def _frame_state(
    batch: list[tuple[int, int, int, int, int, int]],
    *,
    chipno: int = 0,
) -> dict[int, int]:
    """Collapse a frame batch to ``{reg: last_value}`` for one SID chip."""
    state: dict[int, int] = {}
    for _clks, _irq, _nmi, chip, addr, byte in batch:
        if chip != chipno:
            continue
        state[addr & 0x1F] = byte & 0xFF
    return state


def _pydefmon_frame_states(
    prg_path: Path,
    n_frames: int,
    *,
    seed_ram: bytes | None = None,
    seed_base: int = 0,
) -> list[dict[int, int]]:
    """Run pydefmon's DefmonPlayer for ``n_frames`` main frames and
    return per-frame ``{reg: last_value}`` collapses. If ``seed_ram``
    is given, import_runtime_state is called to align pydefmon with a
    live snapshot before frame 0."""
    song = DefmonSong.from_file(str(prg_path))
    player = DefmonPlayer(song)
    if seed_ram is not None:
        player.import_runtime_state(seed_ram, seed_base)
    out: list[dict[int, int]] = []
    for _ in range(n_frames):
        writes = player.play_frame()
        state: dict[int, int] = {}
        for reg_addr, val in writes:
            state[(reg_addr - SID_REG_BASE) & 0x1F] = val & 0xFF
        out.append(state)
    return out


@unittest.skipIf(_skip_reason() is not None, _skip_reason() or "")
class TestPlayerAgainstRealDefmon(unittest.TestCase):
    """Per-frame SID register state pydefmon vs the real defMON player."""

    def test_per_frame_register_state_matches_after_snapshot(self) -> None:
        from defmon_driver.defmon import Defmon  # type: ignore[import-not-found]
        from defmon_driver.tune_manifest import TUNES  # type: ignore[import-not-found]
        from defmon_driver.tune_navigation import (  # type: ignore[import-not-found]
            cursor_load_tune,
        )
        from vice_driver.binmon import BinMon  # type: ignore[import-not-found]

        tune = next((t for t in TUNES if t.name == TUNE_NAME), None)
        self.assertIsNotNone(tune, f"{TUNE_NAME!r} missing from TUNES manifest")

        with tempfile.TemporaryDirectory(prefix="pydefmon-player-") as tmp:
            workdir = Path(tmp)
            shutil.copy2(FIXTURES_DIR / D64_NAME, workdir / "disk.d64")
            dump_path = workdir / "dump" / "sid.dump"
            cid = _start_headlessvice(workdir, dump_path)
            bm = BinMon("127.0.0.1", BINMON_PORT)
            state_ram: bytes = b""
            try:
                time.sleep(1.0)
                bm.connect(timeout=10.0, attempts=80, retry_delay=0.25)
                bm.exit()
                d = Defmon(bm)
                d.wait_for_defmon_loaded(timeout=WAIT_BOOT_TIMEOUT_S)
                cursor_load_tune(d, tune)
                d.play_from_cursor()
                # Let the player settle into steady state under warp
                # (~1s wall = many hundred NMIs) before installing the
                # alignment checkpoint.
                time.sleep(1.0)
                # Halt at $1022 = player_play_body entry. The
                # ``with bm.halted():`` block disables binmon's
                # auto-resume so the CPU stays halted across the
                # mem_get + stat() calls — otherwise every binmon
                # command sends EXIT after, the CPU runs for a few
                # microseconds, and the pitch oscillator (running at
                # the END of the NMI body) re-writes the V0/V1/V2 freq
                # operand slots at $102D/$105E/$108F before we read
                # them. With ``halted()``, the snapshot is atomic with
                # the halt at $1022 and the operand slots are
                # guaranteed to be the same bytes the next post-resume
                # NMI's SID writes will consume.
                bm.run_until_pc(PLAYER_ENTRY_PC, timeout=5.0)
                with bm.halted():
                    state_ram = bm.mem_get(STATE_SNAPSHOT_START, STATE_SNAPSHOT_END)
                    # Punch a sentinel SID write into the dump stream
                    # with side_effects=True so VICE routes the
                    # mem_set through the SID-store callback. Records
                    # AFTER this sentinel in the dump file are
                    # guaranteed post-halt (no libc-buffer lag) — we
                    # locate the sentinel later by scanning for
                    # (reg, value) = (SENTINEL_REG, SENTINEL_VALUE).
                    bm.mem_set(
                        0xD400 + SENTINEL_REG,
                        bytes([SENTINEL_VALUE]),
                        side_effects=True,
                    )
                bm.exit()
                time.sleep(PLAY_DURATION_S)
            finally:
                try:
                    bm.close()
                except Exception:  # noqa: BLE001
                    log.debug("bm.close raised", exc_info=True)
                _stop_container(cid)
                time.sleep(0.2)

            self.assertTrue(
                dump_path.exists(),
                f"dump file {dump_path} was never created",
            )
            records = _parse_dump(dump_path)

        self.assertGreater(
            len(records),
            500,
            f"only {len(records)} SID writes captured; "
            "defMON likely never started playing",
        )

        # Locate the sentinel SID write (reg $15 = $A5, written by
        # bm.mem_set side_effects=True). The first dump record AFTER
        # this is the first SID write from the post-resume CPU; any
        # records before are pre-halt (possibly still in libc buffer
        # when we asked).
        sentinel_idx = next(
            (
                i
                for i, r in enumerate(records)
                if r[3] == 0 and r[4] == SENTINEL_REG and r[5] == SENTINEL_VALUE
            ),
            -1,
        )
        self.assertGreaterEqual(
            sentinel_idx,
            0,
            "sentinel SID write not found in dump — mem_set didn't fire the "
            "SID-store callback, or the dump wasn't flushed",
        )
        post_halt_records = records[sentinel_idx + 1 :]
        captured = _batch_into_frames(post_halt_records)
        log.info(
            "captured %d records (%d post-halt); sentinel at idx %d; "
            "post-halt frames: %d",
            len(records),
            len(post_halt_records),
            sentinel_idx,
            len(captured),
        )

        n = min(COMPARE_FRAMES, len(captured))
        self.assertGreaterEqual(n, 5, f"captured only {n} player frames post-snapshot")

        real_states = [_frame_state(b) for b in captured[:n]]
        pyd_states = _pydefmon_frame_states(
            FIXTURES_DIR / TUNE_PRG,
            n,
            seed_ram=state_ram,
            seed_base=STATE_SNAPSHOT_START,
        )

        mismatches: list[str] = []
        for fi, (real, pyd) in enumerate(zip(real_states, pyd_states)):
            all_regs = sorted(set(real) | set(pyd))
            for reg in all_regs:
                r = real.get(reg)
                p = pyd.get(reg)
                if r != p:
                    addr = SID_REG_BASE + reg
                    if r is not None and p is not None:
                        mismatches.append(
                            f"frame {fi} ${addr:04X}: real=${r:02X} "
                            f"pydefmon=${p:02X}"
                        )
                    else:
                        mismatches.append(
                            f"frame {fi} ${addr:04X}: real={r} pydefmon={p}"
                        )
        if mismatches:
            head = "\n  ".join(mismatches[:25])
            self.fail(
                f"{len(mismatches)} register mismatches across {n} frames "
                f"(first 25):\n  {head}"
            )


if __name__ == "__main__":
    unittest.main()
