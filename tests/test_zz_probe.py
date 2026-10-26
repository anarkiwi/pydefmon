"""TEMPORARY CI probe: diff the Goto80 builds' player code."""

import difflib
import unittest

from pysidtracker import SidImage
from pysidtracker.disasm import disassemble

from pydefmon import _goto80_format as g80
from pydefmon import _sid_format as sf
from tests._support import resolve_tune

TUNES = (
    "MUSICIANS/G/Goto80/Rent-A-Cop.sid",
    "MUSICIANS/G/Goto80/Rent-A-Cop_Reloaded.sid",
    "MUSICIANS/G/Goto80/Rent-A-Cop_Reloaded_title.sid",
    "MUSICIANS/G/Goto80/Evil_Wizard_2.sid",
)


def _rows(rel):
    image = SidImage.from_bytes(resolve_tune(rel).read_bytes())
    sig = sf.find_signature(image.mem)
    step = g80.find_tables(image.mem)
    lo = sig - 0x30
    hi = sig + 0x2F0
    rows = []
    for a, b, t in disassemble(image, lo, hi):
        # drop absolute operands that differ per relocation: keep opcode + length
        rows.append(f"{b[0]:02X}/{len(b)} {t.split()[0]}  @{a - sig:+04X}")
    return sig, rows


class TestProbe(unittest.TestCase):
    def test_probe(self):
        base_sig, base = _rows(TUNES[0])
        out = [f"base {TUNES[0]} sig={base_sig:04X} rows={len(base)}"]
        for rel in TUNES[1:]:
            sig, rows = _rows(rel)
            out.append(f"== {rel} sig={sig:04X} rows={len(rows)}")
            key = lambda r: r.split("@")[0]
            sm = difflib.SequenceMatcher(
                None, [key(r) for r in base], [key(r) for r in rows], autojunk=False
            )
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag != "equal":
                    out.append(f" {tag} base[{base[i1:i2]}] -> [{rows[j1:j2]}]")
        self.fail("\n" + "\n".join(out))


if __name__ == "__main__":
    unittest.main()
