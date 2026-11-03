"""Whole-routine trials: a random replay state run once on the CPU and once as the spec.

The routine's code is the universe: every instruction reachable from the play entries and
every branch's two ways. A trial writes a random state into every variable of the player
and the tables its code reads, within what the player's own data can hold (opcode bytes hold
the opcodes the code patches in, arranger rows are play rows or jumps to play rows, pointers
lead into a pool of random stream bytes), binds the spec to that memory, runs the routine from
an entry on a jennings :class:`~jennings.coverage._core.CoverageMPU` and the spec on a copy of
the memory, and reports which of the code the CPU ran and where the SID and the variables
differ.
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
from pydefmon.spec import (
    READER_STRIDE,
    defmon_binding,
    entry_base,
    player_places,
    spec as play_spec,
    state_addresses,
    variable_addresses,
)

from tests._support import audible, replay_state

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
    ("up := carry(cut, cut_step);", "up := false;"),
    ("if neg(out) || out < bottom {", "if neg(out) {"),
    ("loop_left := column3[y];", "loop_left := column3[y] + 1;"),
    ("ptr += u16(steps.pos);", "ptr += u16(steps.pos) + 1;"),
    ("counter := flag & 0x0F;", "counter := flag & 0x07;"),
    ("pos := n + 1;", "pos := n;"),
    ("if rows[n] < 0x100 {", "if rows[n] < 0x80 {"),
    ("res := res & L.vmask[k % 3];", "res := res;"),
    (
        "voice[k % 3].pw := word(w & 0xF0, w);",
        "voice[k % 3].pw := word(w & 0xE0, w);",
    ),
    ("voice[k % 3].mode := next bytes;", "voice[k % 3].mode := 0;"),
    ("cut_step := word(lo, hi & 0x3F);", "cut_step := word(lo, hi & 0x7F);"),
    ("cut := cut - cut_step - 1;", "cut := cut - cut_step;"),
    ("pw := pw - u16(depth) - u16(!cy);", "pw := pw - u16(depth);"),
    ("freq := base + delta;", "freq := base + delta + 1;"),
)


def mutated_spec(old: str, new: str):
    """The spec with the one change ``old`` -> ``new``."""
    from pysidtracker.playspec import parse  # pylint: disable=import-outside-toplevel
    from pydefmon.spec import (
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
        self.spec = spec or play_spec()
        self.view = self.player.view()
        self.entries = sorted(
            self.player._entries()
        )  # pylint: disable=protected-access
        self.mem0 = bytes(self.player._mem)  # pylint: disable=protected-access
        image = SidImage.from_bytes(data)
        self.pool = _pool(image.load, image.load + len(image.image))
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

    def trial(self, seed: int) -> Tuple[CoverageMap, List[str]]:
        """Run ``seed``'s state from its entry on the CPU and as the spec; return the
        CPU's coverage and what the two leave differently: SID registers by address and
        variables by name."""
        # pylint: disable=protected-access
        rng = random.Random(seed)
        entry = self.entries[seed % len(self.entries)]
        player = self.player
        mem, bus, mpu = player._mem, player._obs, player._mpu
        mem[:] = self.mem0
        base = entry_base(self.mem0, entry)
        places = player_places(self.mem0, base, entry)
        self._randomize(rng, places, mem, bus)
        before = bytes(mem)
        binding = defmon_binding(before, base, entry, places)
        program = elaborate(self.spec, binding)
        frame, state = compile_program(program), controls(program)
        spec_mem = bytearray(mem)
        self.sid_writes.clear()
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
        sid = binding["sid"]
        heard = tuple(
            self.sid_writes.get(at, before[at]) for at in range(sid, sid + 0x19)
        )
        differing = []
        if audible(heard) != audible(spec_mem[sid : sid + 0x19]):
            differing.append("sid")
        have = {
            program.keys[name]: state[i]
            for i, (name, *_rest) in enumerate(program.controls)
        }
        differing += [
            key for key, value in replay_state(mem, entry).items() if have[key] != value
        ]
        if not binding["cp_extra"] and mem[binding["cp"]] != spec_mem[binding["cp"]]:
            differing.append("cp")
        return cpu.cov, differing

    def _randomize(self, rng: random.Random, places, mem, bus) -> None:
        """Write a random state into the player's variables and tables and the pool streams."""
        low, high = self.pool

        def byte() -> int:
            return rng.choice(EDGES) if rng.random() < 0.5 else rng.randrange(256)

        def pointer() -> int:
            at = rng.randrange(low, high - 0x200)
            if rng.random() < 0.3:
                at = at & 0xFF00 | rng.choice((0xFC, 0xFD, 0xFE, 0xFF))
            return at

        mem[low:high] = bytes(byte() for _ in range(high - low))
        for address in variable_addresses(places):
            bus[address] = byte()
        opcode = rng.choice(CUTOFF_OPCODES)
        bus[places["op_lo"]] = bus[places["op_hi"]] = opcode
        if places["shifts"]:
            bus[places["shift"]] = rng.choice(SHIFT_OPCODES)
        if places["floored"]:
            bus[places["floor"]] = byte()
        addresses = state_addresses(places)
        for x in range(3):
            where = pointer()
            lo, hi = addresses[f"reader[{x}].ptr"]
            bus[lo], bus[hi] = where & 0xFF, where >> 8
            flag = mem[where]
            at = x * READER_STRIDE
            bus[places["flag"] + at] = flag
            for name, shift in (("gate_a", 1), ("gate_b", 2), ("gate_n", 3)):
                bus[places[name] + at] = flag << shift & 0xFF
            for name in ("copy1", "copy2", "copy3"):
                bus[places[name + "_lo"] + at], bus[places[name + "_hi"] + at] = (
                    where & 0xFF,
                    where >> 8,
                )
        self._tables(rng, places, bus, byte)
        self._arranger(rng, places, bus, byte)
        if not places["uncounted"]:
            bus[places["owner"]] = rng.randrange(256)
            bus[places["count"]] = rng.choice((0, 1, 1, 2, 3, byte()))
            if rng.random() < 0.5:
                bus[places["owner"]] = bus[places["cursor"]]

    def _tables(self, rng, places, bus, byte) -> None:
        """The sidTAB rows: addresses into the pool, or jumps to a row that has one."""
        low, high = self.pool
        direct = [y for y in range(256) if rng.random() >= 0.15] or [0]
        for y in range(256):
            bus[places["dl"] + y] = byte()
            if y in direct:
                bus[places["rows_lo"] + y] = rng.randrange(256)
                bus[places["rows_hi"] + y] = rng.randrange(low >> 8, (high >> 8) - 2)
            else:
                bus[places["rows_lo"] + y] = rng.choice(direct)
                bus[places["rows_hi"] + y] = 0

    def _arranger(self, rng, places, bus, byte) -> None:
        """Arranger rows are play rows (three pattern numbers below ``$80``) or jumps
        (``$80`` or more, to a play row), and the pattern tables lead into the pool."""
        low, high = self.pool
        play = [rng.random() < 0.7 for _ in range(256)]
        rows = [y for y in range(256) if play[y]] or [0]
        play[rows[0]] = True
        for y in range(256):
            if play[y]:
                for name in ("v1", "v2", "v3"):
                    bus[places[name] + y] = rng.randrange(0x80)
            else:
                bus[places["v1"] + y] = rng.randrange(0x80, 0x100)
                bus[places["v2"] + y] = rng.choice(rows)
                bus[places["v3"] + y] = byte()
        for k in range(0x80):
            bus[places["pat_lo"] + k] = rng.randrange(256)
            bus[places["pat_hi"] + k] = rng.randrange(low >> 8, (high >> 8) - 2)


def _pool(load: int, end: int) -> Tuple[int, int]:
    """A page-aligned span of free RAM below ``$A000`` the random streams live in."""
    for start in range(0x2000, 0xA000 - POOL_SIZE + 1, 0x1000):
        if end <= start or start + POOL_SIZE <= load:
            return start, start + POOL_SIZE
    raise ValueError("no free span for the stream pool")
