"""Clean-room py65 register oracle for defMON ``.sid`` replays (test-only).

Runs a PSID/RSID replay's init then its play routine at PAL frame rate on a
py65 6502, sampling all 25 SID registers at the end of each play call to
produce the ground-truth per-frame register grid the Goto80 decoder is
checked against. No copyrighted emulator/player source is vendored; py65 is a
clean-room 6502 and the NMOS illegal opcodes defMON uses (SBX/SAX/ANC/SBC/LAX
and the ALR/ARR immediates) are implemented here from their documented
behaviour.

Requires the ``emu`` extra (``pip install pydefmon[emu]``); importing raises
``ImportError`` when py65 is absent so callers can ``skipTest`` cleanly.
"""

import struct
from typing import List

from py65.devices.mpu6502 import MPU  # noqa: F401  (import-time availability gate)
from py65.memory import ObservableMemory

SID_BASE = 0xD400
SID_END = 0xD41C  # $D400..$D41C inclusive (25 registers)


def _patch_illegals(mpu) -> None:
    """Install the NMOS illegal opcodes defMON's replay executes."""
    mpu.instruct = list(mpu.instruct)
    mpu.cycletime = list(mpu.cycletime)
    mpu.extracycles = list(mpu.extracycles)

    def _set(op, fn, cyc=2):
        mpu.instruct[op] = fn
        mpu.cycletime[op] = cyc
        mpu.extracycles[op] = 0

    def i_sbx(self):  # SBX/AXS #imm: X = (A & X) - imm, CMP-style carry
        v = self.ByteAt(self.ProgramCounter())
        t = (self.a & self.x) - v
        self.x = t & 0xFF
        self.p &= ~(self.CARRY | self.ZERO | self.NEGATIVE)
        if t >= 0:
            self.p |= self.CARRY
        self.FlagsNZ(self.x)
        self.pc += 1

    def i_anc(self):  # ANC #imm: A &= imm; C = bit7
        self.a &= self.ByteAt(self.ProgramCounter())
        self.FlagsNZ(self.a)
        self.p = (self.p & ~self.CARRY) | (1 if self.a & 0x80 else 0)
        self.pc += 1

    def i_alr(self):  # ALR #imm: A = (A & imm) >> 1
        self.a &= self.ByteAt(self.ProgramCounter())
        self.p = (self.p & ~self.CARRY) | (self.a & 1)
        self.a >>= 1
        self.FlagsNZ(self.a)
        self.pc += 1

    def i_arr(self):  # ARR #imm
        self.a &= self.ByteAt(self.ProgramCounter())
        c = 1 if self.p & self.CARRY else 0
        self.a = (self.a >> 1) | (c << 7)
        self.FlagsNZ(self.a)
        self.p &= ~(self.CARRY | self.OVERFLOW)
        if self.a & 0x40:
            self.p |= self.CARRY
        if bool(self.a & 0x40) ^ bool(self.a & 0x20):
            self.p |= self.OVERFLOW
        self.pc += 1

    def i_sbc(self):  # SBC #imm alias ($EB)
        self.opSBC(self.ProgramCounter)
        self.pc += 1

    def i_lax_imm(self):  # LAX #imm -> A = X = imm
        v = self.ByteAt(self.ProgramCounter())
        self.a = self.x = v
        self.FlagsNZ(v)
        self.pc += 1

    def _sax(meth, pcadd):  # SAX: store A & X
        def f(self):
            self.memory[getattr(self, meth)()] = self.a & self.x
            self.pc += pcadd

        return f

    def _lax(meth, pcadd):  # LAX: A = X = mem
        def f(self):
            v = self.ByteAt(getattr(self, meth)())
            self.a = self.x = v
            self.FlagsNZ(v)
            self.pc += pcadd

        return f

    _set(0xCB, i_sbx)
    _set(0x0B, i_anc)
    _set(0x2B, i_anc)
    _set(0x4B, i_alr)
    _set(0x6B, i_arr)
    _set(0xEB, i_sbc)
    _set(0xAB, i_lax_imm)
    _set(0x83, _sax("IndirectXAddr", 1), 6)
    _set(0x87, _sax("ZeroPageAddr", 1), 3)
    _set(0x8F, _sax("AbsoluteAddr", 2), 4)
    _set(0x97, _sax("ZeroPageYAddr", 1), 4)
    _set(0xA3, _lax("IndirectXAddr", 1), 6)
    _set(0xA7, _lax("ZeroPageAddr", 1), 3)
    _set(0xAF, _lax("AbsoluteAddr", 2), 4)
    _set(0xB3, _lax("IndirectYAddr", 1), 5)
    _set(0xB7, _lax("ZeroPageYAddr", 1), 4)
    _set(0xBF, _lax("AbsoluteYAddr", 2), 4)
    # Multi-byte NOP illegals defMON's data-adjacent code can drift through.
    for op in (0x1A, 0x3A, 0x5A, 0x7A, 0xDA, 0xFA):
        _set(op, (lambda s: setattr(s, "pc", s.pc)), 2)
    for op in (0x80, 0x82, 0x89, 0xC2, 0xE2):
        _set(op, (lambda s: setattr(s, "pc", s.pc + 1)), 2)
    for op in (0x04, 0x44, 0x64):
        _set(op, (lambda s: setattr(s, "pc", s.pc + 1)), 3)
    for op in (0x14, 0x34, 0x54, 0x74, 0xD4, 0xF4):
        _set(op, (lambda s: setattr(s, "pc", s.pc + 1)), 4)
    _set(0x0C, (lambda s: setattr(s, "pc", s.pc + 2)), 4)
    for op in (0x1C, 0x3C, 0x5C, 0x7C, 0xDC, 0xFC):
        _set(op, (lambda s: setattr(s, "pc", s.pc + 2)), 4)


def parse_psid(raw: bytes) -> dict:
    """Parse a PSID/RSID header into the fields the oracle needs."""
    if raw[0:4] not in (b"PSID", b"RSID"):
        raise ValueError(f"not a PSID/RSID: {raw[0:4]!r}")
    doff, load, init, play = struct.unpack(">HHHH", raw[6:14])
    start = struct.unpack(">H", raw[16:18])[0]
    body = raw[doff:]
    if load == 0:
        load = body[0] | (body[1] << 8)
        body = body[2:]
    return {"load": load, "init": init, "play": play, "startsong": start, "body": body}


class Oracle:
    """PC-guided PSID emulator producing the per-frame SID register grid."""

    def __init__(self, raw: bytes) -> None:
        self.psid = parse_psid(raw)
        self.mem = ObservableMemory()
        for i, byte in enumerate(self.psid["body"]):
            self.mem[self.psid["load"] + i] = byte
        self.mpu = MPU(memory=self.mem)
        _patch_illegals(self.mpu)
        # Minimal VIC/SID read model so raster-sync + SID-type detect in the
        # replay's init/play terminate (the harness has no VIC/SID).
        self.mem.subscribe_to_read([0xD011, 0xD012], self._on_raster)
        self.mem.subscribe_to_read([0xD41B, 0xD41C], self._on_sidread)

    def _on_raster(self, addr):
        line = (self.mpu.processorCycles // 63) % 312
        if addr == 0xD012:
            return line & 0xFF
        return (self.mem._subject[0xD011] & 0x7F) | (((line >> 8) & 1) << 7)

    def _on_sidread(self, addr):  # pylint: disable=unused-argument
        return (self.mpu.processorCycles >> 3) & 0xFF

    def _run(self, pc, a=0, max_steps=4_000_000):
        m = self.mpu
        ret = 0x0100
        m.memory[0x01FF] = ((ret - 1) >> 8) & 0xFF
        m.memory[0x01FE] = (ret - 1) & 0xFF
        m.sp = 0xFD
        m.pc, m.a, m.x, m.y = pc, a, 0, 0
        for _ in range(max_steps):
            if m.pc == ret:
                return
            m.step()
        raise RuntimeError("oracle step budget exceeded (runaway routine?)")

    def grid(self, frames: int) -> List[List[int]]:
        """Return ``frames`` rows of the 25 SID registers ($D400..$D41C)."""
        self._run(self.psid["init"], a=self.psid["startsong"] - 1)
        out: List[List[int]] = []
        for _ in range(frames):
            self._run(self.psid["play"])
            out.append([self.mem[a] for a in range(SID_BASE, SID_END + 1)])
        return out
