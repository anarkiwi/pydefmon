"""``_goto80_format.find_tables``: the table sites a Goto80 player's code leads to."""

import unittest

from pysidtracker import SidImage

from pydefmon import _goto80_format as g80
from pydefmon._sid_format import SIGNATURE

SIG = 0x9025
VOICE2 = SIG + 0x31
VOICE3 = SIG + 0x62
ORDERLIST = SIG + 0xC9
STEPPER = ORDERLIST + 0x1E3

TABLES = {
    "sidtab_lo": 0x979D,
    "sidtab_hi": 0x989D,
    "sidtab_dl": 0x9D1B,
    "patptr_lo": 0x999D,
    "patptr_hi": 0x99DC,
    "arr_v0": 0x9A1B,
    "arr_v1": 0x9B1B,
    "arr_v2": 0x9C1B,
}


def _w(addr):
    return [addr & 0xFF, addr >> 8]


def _put(mem, addr, data):
    mem[addr : addr + len(data)] = bytes(data)


def _player(*, voice2=VOICE2, order_gap=0):
    mem = bytearray(0x10000)
    t = TABLES
    sig = [0 if b is None else b for b in SIGNATURE]
    _put(mem, SIG, sig + _w(voice2))
    v2 = (
        "A2 00 A9 00 8E 09 D4 8D 0A D4 A2 00 A9 00 8E 07 D4 8D 08 D4 "
        "A2 00 A0 00 A9 00 49 00 8E 0D D4 8C 0C D4 8D 0B D4 4C"
    )
    _put(mem, VOICE2, bytes.fromhex(v2.replace(" ", "")) + bytes(_w(VOICE3)))

    def voice(arr=None):
        pre = [0xBE, *_w(arr)] if arr else []
        return (
            pre
            + [0xBD, *_w(t["patptr_lo"]), 0x8D, 0, 0]
            + [0xBD, *_w(t["patptr_hi"]), 0x8D, 0, 0]
        )

    order = [
        0xA0,
        0xFF,
        0xBE,
        *_w(t["arr_v0"]),
        0x10,
        0x07,
        0xB9,
        *_w(t["arr_v1"]),
        0xA8,
    ]
    order += [0xBE, *_w(t["arr_v0"])] + voice()
    order += voice(t["arr_v1"]) + voice(t["arr_v2"])
    _put(mem, ORDERLIST + order_gap, order)
    _put(
        mem,
        STEPPER,
        [0xB9, *_w(t["sidtab_hi"]), 0xD0, 0x07, 0xB9, *_w(t["sidtab_lo"]), 0xA8]
        + [0xB9, 0, 0, 0x85, 0xFC, 0xB9, *_w(t["sidtab_dl"])],
    )
    return SidImage(mem, 0x9000, 0x9500, None, bytes(mem[0x9000:0x9500]), b"")


class TestFindTables(unittest.TestCase):
    def test_finds_the_tables(self):
        self.assertEqual(g80.find_tables(_player(), SIG), TABLES)

    def test_orderlist_position_is_not_fixed(self):
        # a build with extra instructions ahead of the orderlist still resolves
        self.assertEqual(g80.find_tables(_player(order_gap=1), SIG), TABLES)

    def test_broken_chain_is_not_goto80(self):
        self.assertIsNone(g80.find_tables(_player(voice2=VOICE2 + 1), SIG))

    def test_no_stepper_is_not_goto80(self):
        image = _player()
        image.mem[STEPPER : STEPPER + 3] = bytes(3)
        self.assertIsNone(g80.find_tables(image, SIG))


if __name__ == "__main__":
    unittest.main()
