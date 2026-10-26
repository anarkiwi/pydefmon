"""TEMPORARY CI probe: can the data base be read from the player code?"""

import unittest

from pysidtracker import CodePattern, SidImage, find_code_first

from pydefmon import _sid_format as sf
from tests._support import resolve_corpus

STEPPER = CodePattern(
    "B9 {sidtab_hi:w} D0 07 B9 {sidtab_lo:w} A8 B9 ?? ?? 85 FC B9 {sidtab_dl:w}"
)


class TestProbe(unittest.TestCase):
    def test_probe(self):
        out = []
        for rel, path in resolve_corpus():
            image = SidImage.from_bytes(path.read_bytes())
            sig = sf.find_signature(image.mem)
            hit = find_code_first(image.mem, STEPPER)
            base = sig + 0x7DE
            if hit is None:
                out.append(f"NOHIT {rel} sig={sig:04X}")
                continue
            c = hit.captures
            out.append(
                f"{'OK ' if c['sidtab_lo'] == base else 'BAD'} {rel} base={base:04X} "
                f"lo={c['sidtab_lo']:04X} hi={c['sidtab_hi']:04X} dl={c['sidtab_dl']:04X} "
                f"hit={hit.addr:04X} sig={sig:04X}"
            )
        from tests._support import editor_image_bytes, fixture_path

        try:
            ed = editor_image_bytes()
            out.append(f"EDITOR {None if ed is None else len(ed)}")
        except Exception as exc:  # pylint: disable=broad-except
            out.append(f"EDITOR raised {exc!r}")
        try:
            out.append(f"FIXTURE {fixture_path('dansa_in.prg')}")
        except Exception as exc:  # pylint: disable=broad-except
            out.append(f"FIXTURE raised {exc!r}")
        self.fail("\n" + "\n".join(out))


if __name__ == "__main__":
    unittest.main()
