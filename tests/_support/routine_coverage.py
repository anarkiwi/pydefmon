"""Whole-routine trials: a random replay state run once on the CPU and once as the spec.

The routine's code is the universe: every instruction reachable from the play entries and
every branch's two ways. A trial writes a random state into every cell the spec binds,
within what the player's own data can hold (opcode cells hold the opcodes the code
patches in, arranger rows are play rows or jumps to play rows, pointers lead into a pool
of random stream bytes), runs the routine from an entry on a jennings
:class:`~jennings.coverage._core.CoverageMPU` and the spec on a copy of the memory, and
reports which of the code the CPU ran and where the 64 KiB outside the stack page and the
SID differ.
"""

from __future__ import annotations

import random
from typing import Dict, List, Set, Tuple

from jennings.coverage._core import (
    EXEC,
    FALLTHRU,
    TAKEN,
    CoverageMap,
    CoverageMPU,
    is_branch,
)
from jennings.coverage.cfg import successors
from pysidtracker import SidImage
from pysidtracker.emu import run_to_rts
from pysidtracker.playspec import compile_program, elaborate
from pysidtracker.playspec.pyexec import controls

from pydefmon import DefmonPlayer
from pydefmon._spec_layout import defmon_binding, entry_base
from pydefmon.defmon_player import _spec

#: Values a byte is drawn from half the time: the edges the routine compares against.
EDGES = (0, 0, 1, 2, 0x7F, 0x80, 0xFF, 0x40, 0x20, 0x10, 0x08, 0x01, 0xFE, 0x0F, 0x0E)
#: The opcodes the cutoff block patches in, and the shift it leaves or patches out.
CUTOFF_OPCODES = (0x69, 0xE9)
SHIFT_OPCODES = (0x0A, 0xEA)
POOL_SIZE = 0x2000
#: How far past a player's start its code and tables reach.
REACH = 0x900
STACK = range(0x100, 0x200)
SID_PAGE = range(0xD000, 0xE000)
MAX_CYCLES = 2_000_000

Feature = Tuple[int, int]

#: Single changes to the spec that the states must tell apart from it: coverage shows the
#: code ran, not that its result mattered.
MUTANTS = (
    ("c := carry(cut_lo, cut_step_lo);", "c := false;"),
    ("if neg(out) || out < bottom {", "if neg(out) {"),
    ("count := v3[y];", "count := v3[y] + 1;"),
    ("ptr := ptr + u16(y);", "ptr := ptr + u16(y) + 1;"),
    ("counter := fl & 0x0F;", "counter := fl & 0x07;"),
    ("blk_rowidx := eff + 1;", "blk_rowidx := eff;"),
    ("if rows_hi[eff] == 0 {", "if rows_hi[eff] == 1 {"),
    ("res := res & mem[L.vmask + v];", "res := res;"),
    ("mem[L.pw_lo + v] := w & 0xF0;", "mem[L.pw_lo + v] := w & 0xE0;"),
    (
        "if low & 0x04 != 0 { mem[L.mode + v] := next r; }",
        "if low & 0x04 != 0 { mem[L.mode + v] := 0; }",
    ),
    ("cut_step_hi := ahi & 0x3F;", "cut_step_hi := ahi & 0x7F;"),
    ("cut_hi := cut_hi - cut_step_hi - u8(!cin);", "cut_hi := cut_hi - cut_step_hi;"),
)


def mutated_spec(old: str, new: str):
    """The spec with the one change ``old`` -> ``new``."""
    from pysidtracker.playspec import parse  # pylint: disable=import-outside-toplevel
    from pydefmon._spec_layout import (
        spec_text,
    )  # pylint: disable=import-outside-toplevel

    text = spec_text("defmon")
    assert old in text, old
    return parse(text.replace(old, new, 1))


class Routine:
    """The play routine of one replay: its code universe and the means to run trials."""

    def __init__(self, data: bytes, subtune: "int | None" = None, spec=None):
        self.player = DefmonPlayer(data, subtune=subtune)
        assert self.player.spec_backed
        self.view = self.player.view()
        self.entries = sorted(
            self.player._entries()
        )  # pylint: disable=protected-access
        self.mem0 = bytes(self.player._mem)  # pylint: disable=protected-access
        image = SidImage.from_bytes(data)
        self.pool = _pool(image.load, image.load + len(image.image))
        self.programs: Dict[int, tuple] = {}
        self.bindings: Dict[int, dict] = {}
        for entry in self.entries:
            base = entry_base(self.view, entry)
            binding = defmon_binding(self.view, base, entry)
            program = elaborate(spec or _spec(), binding)
            self.bindings[entry] = binding
            self.programs[entry] = (
                program,
                compile_program(program),
                controls(program),
            )
        self.sid_writes: Dict[int, int] = {}
        self.player._mpu.memory.subscribe_to_write(  # pylint: disable=protected-access
            range(0xD400, 0xD800), self._on_sid_write
        )
        self.instructions, self.branches = self._universe()

    def _on_sid_write(self, address: int, value: int) -> None:
        self.sid_writes[address] = value

    def _universe(self) -> Tuple[Set[int], Set[int]]:
        """Every instruction reachable from the entries within the player, and the branches."""
        reached: Set[int] = set()
        branches: Set[int] = set()
        for entry in self.entries:
            base = entry_base(self.view, entry)
            stack = [entry]
            while stack:
                at = stack.pop()
                if at in reached or not base - 3 <= at < base + REACH:
                    continue
                reached.add(at)
                following, _jump, callee = successors(self.view, at)
                if is_branch(self.view[at]):
                    branches.add(at)
                stack.extend(target for _flag, target in following)
                if callee is not None:
                    stack.append(callee)
        return reached, branches

    def features(self) -> Set[Feature]:
        """Every ``(address, flag)`` a trial can show: each instruction run, each way of
        each branch."""
        out = {(at, EXEC) for at in self.instructions}
        for at in self.branches:
            out |= {(at, TAKEN), (at, FALLTHRU)}
        return out

    def shown(self, cov: CoverageMap) -> Set[Feature]:
        """The features ``cov`` shows."""
        flags = cov.flags
        return {(at, flag) for at, flag in self.features() if flags[at] & flag}

    def trial(self, seed: int) -> Tuple[CoverageMap, List[int]]:
        """Run ``seed``'s state from its entry on the CPU and as the spec; return the
        CPU's coverage and the addresses the two leave differently."""
        # pylint: disable=protected-access
        rng = random.Random(seed)
        entry = self.entries[seed % len(self.entries)]
        player = self.player
        mem, bus, mpu = player._mem, player._obs, player._mpu
        mem[:] = self.mem0
        self._randomize(rng, entry, mem, bus)
        spec_mem = bytearray(mem)
        before = bytes(mem)
        self.sid_writes.clear()
        program, frame, state = self.programs[entry]
        frame(
            spec_mem.__getitem__,
            spec_mem.__setitem__,
            state,
            None,
            {name[4:]: 0 for name in program.inputs},
        )
        cpu = CoverageMPU(memory=mpu.memory)
        cpu.sp = 0xFF
        run_to_rts(cpu, bus, entry, 0, MAX_CYCLES)
        differing = [
            at
            for at in range(0x10000)
            if at not in STACK and at not in SID_PAGE and mem[at] != spec_mem[at]
        ]
        sid = self.bindings[entry]["sid"]
        for at in range(sid, sid + 0x19):
            if self.sid_writes.get(at, before[at]) != spec_mem[at]:
                differing.append(at)
        return cpu.cov, differing

    def _randomize(self, rng: random.Random, entry: int, mem, bus) -> None:
        """Write a random state into the cells the spec binds and the pool streams."""
        binding = self.bindings[entry]
        program = self.programs[entry][0]
        scheduled = set(program.order) | {""}
        low, high = self.pool

        def byte() -> int:
            return rng.choice(EDGES) if rng.random() < 0.5 else rng.randrange(256)

        def pointer() -> int:
            at = rng.randrange(low, high - 0x200)
            if rng.random() < 0.3:
                at = at & 0xFF00 | rng.choice((0xFC, 0xFD, 0xFE, 0xFF))
            return at

        mem[low:high] = bytes(byte() for _ in range(high - low))
        opcode = rng.choice(CUTOFF_OPCODES)
        for cell in program.cells:
            if (
                cell.owner not in scheduled
                and cell.owner.split("[")[0] not in scheduled
            ):
                continue
            if cell.name in ("v1", "v2", "v3", "sid"):
                continue
            if cell.name == "floor" and not binding["floored"]:
                continue
            if cell.name == "shift" and not binding["shifts"]:
                continue
            if cell.name in ("owner", "count") and binding["uncounted"]:
                continue
            if len(cell.place) == 2:
                for i in range(cell.length or 1):
                    where = pointer()
                    bus[cell.place[0] + i], bus[cell.place[1] + i] = (
                        where & 0xFF,
                        where >> 8,
                    )
                continue
            for at in cell.extent():
                if cell.name in ("op_lo", "op_hi"):
                    bus[at] = opcode
                elif cell.name == "shift":
                    bus[at] = rng.choice(SHIFT_OPCODES)
                elif cell.name == "rows_hi":
                    bus[at] = (
                        0
                        if rng.random() < 0.15
                        else rng.randrange(low >> 8, (high >> 8) - 2)
                    )
                else:
                    bus[at] = byte()
        self._arranger(rng, binding, bus, byte)
        if not binding["uncounted"]:
            bus[binding["owner"]] = rng.randrange(256)
            bus[binding["count"]] = rng.choice((0, 1, 1, 2, 3, byte()))
            if rng.random() < 0.5:
                bus[binding["owner"]] = bus[binding["cursor"]]

    def _arranger(self, rng, binding, bus, byte) -> None:
        """Arranger rows are play rows (three pattern numbers below ``$80``) or jumps
        (``$80`` or more, to a play row), and the pattern tables lead into the pool."""
        low, high = self.pool
        play = [rng.random() < 0.7 for _ in range(256)]
        rows = [y for y in range(256) if play[y]] or [0]
        play[rows[0]] = True
        for y in range(256):
            if play[y]:
                for name in ("v1", "v2", "v3"):
                    bus[binding[name] + y] = rng.randrange(0x80)
            else:
                bus[binding["v1"] + y] = rng.randrange(0x80, 0x100)
                bus[binding["v2"] + y] = rng.choice(rows)
                bus[binding["v3"] + y] = byte()
        for k in range(0x80):
            bus[binding["pat_lo"] + k] = rng.randrange(256)
            bus[binding["pat_hi"] + k] = rng.randrange(low >> 8, (high >> 8) - 2)


def _pool(load: int, end: int) -> Tuple[int, int]:
    """A page-aligned span of free RAM below ``$A000`` the random streams live in."""
    for start in range(0x2000, 0xA000 - POOL_SIZE + 1, 0x1000):
        if end <= start or start + POOL_SIZE <= load:
            return start, start + POOL_SIZE
    raise ValueError("no free span for the stream pool")
