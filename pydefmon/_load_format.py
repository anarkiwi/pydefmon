"""defMON's ``$D6C9`` LOAD-time codec — RLE decoder + encoder.

Private to :mod:`pydefmon`; reach for :class:`pydefmon.DefmonSong` to
read/write defMON ``.prg`` files. The codec sits underneath
:meth:`DefmonSong.from_bytes` / :meth:`DefmonSong.to_bytes` and
matches the real defMON loader byte-for-byte (a freshly-encoded
tune is byte-loadable by the original binary).

Two entry points:

* :func:`decode_load_stream` — parses a defMON PRG body into a
  ``{addr: byte}`` write map. Inverse of the on-chip ``$D6C9``
  backward-walking decoder.
* :func:`encode_ram_block` — builds a defMON-loadable PRG (load
  address header + body) that decodes back to a given RAM block.
  Greedy backward emission; the encoder picks any valid encoding
  (defMON's own SAVE picks edit-history-dependent encodings that
  pydefmon doesn't reproduce — both are valid PRGs that load to
  the same RAM image).
"""

from __future__ import annotations

LOAD_ADDR = 0x1800
ESC = 0xFF


class CodecError(ValueError):
    """Raised on malformed input to decode or encode."""


# ---- decode -----------------------------------------------------------


def decode_load_stream(
    body: bytes,
    src_end_addr: int,
    src_floor: int,
    dest_start: int,
    max_iters: int = 1_000_000,
) -> tuple[dict[int, int], int]:
    """Apply the $D6C9 backward RLE decoder to ``body``.

    Args:
      body: file body bytes as loaded into RAM at $1800.
      src_end_addr: starting src address, = body_end_addr - 5
                    (= $1800 + len(body) - 5).
      src_floor: src must stay >= src_floor; below it, decoder exits.
      dest_start: dest start address; walks downward from here.

    Returns ``(writes_by_addr, iterations)``.
    """
    body_start = 0x1800
    body_len = len(body)

    def read(addr: int) -> int | None:
        offset = addr - body_start
        if 0 <= offset < body_len:
            return body[offset]
        return None

    src = src_end_addr
    dest = dest_start
    writes: dict[int, int] = {}
    it = 0

    for it in range(max_iters):
        # defMON's $D6C9 termination check ($D6D5-$D6E1):
        # SEC + SBC of src against (src_floor>>8 << 8 | src_floor&FF) — operand
        # bytes at $D6D9 / $D6E0 patched to src_floor's lo/hi BEFORE the JSR
        # $D6C9 call (see $CED7-$CEDE). BCS taken iff src >= src_floor (no
        # borrow). When BCS is not taken, the fall-through path at
        # $D6E3-$D6E9 can still do ONE more FF-escape if src_lo - src_floor_lo
        # >= $FE (i.e., src in [$17FE, $17FF] for src_floor=$1800). Practical
        # outcome verified by `harness/probe_jp_target_origin.py` on
        # .AUTOMATAS2017: defMON keeps iterating down to src=$1800 (and
        # occasionally one step below via the FF-escape continuation),
        # whereas this decoder previously stopped at src=$1802 — missing
        # the iterations that synthesise the JP-target bytes in $1800-$18FF.
        if src < src_floor:
            break
        b0 = read(src + 0)
        b1 = read(src + 1)
        b2 = read(src + 2)
        if b0 is None or b1 is None or b2 is None:
            break

        if b1 != ESC:
            writes[dest] = b2
            dest -= 1
            src -= 1
        elif b2 == ESC:
            writes[dest] = ESC
            dest -= 1
            src -= 2
        elif b0 == ESC:
            writes[dest] = b2
            dest -= 1
            src -= 1
        else:
            count = b2 + 2
            for _ in range(count):
                writes[dest] = b0
                dest -= 1
            src -= 3
    else:
        raise CodecError(f"decode hit max_iters={max_iters}")

    return writes, it + 1


# ---- encode -----------------------------------------------------------


def encode_load_stream(target: bytes | bytearray, dest_start: int) -> bytes:
    """Return a PRG (load-addr-prefixed) that decodes to ``target`` at
    ``dest_start..dest_start - len(target) + 1`` via the $D6C9 decoder.

    ``target[0]`` lands at ``dest_start``; ``target[i]`` at
    ``dest_start - i``.
    """
    if not target:
        raise CodecError("empty target")
    if not 0x1800 <= dest_start <= 0xFFFF:
        raise CodecError(f"dest_start ${dest_start:04X} out of range")
    if dest_start - (len(target) - 1) < LOAD_ADDR:
        raise CodecError(
            f"target span would underflow load addr: "
            f"dest_start=${dest_start:04X}, len={len(target)}"
        )
    dest_hi_byte = (dest_start >> 8) - 0x18
    if not 0 <= dest_hi_byte <= 0xFF:
        raise CodecError(
            f"dest_hi out of range: ${dest_start:04X} -> {dest_hi_byte:#x}"
        )

    D = target
    N = len(D)
    S: dict[int, int] = {}
    cost_sum = 0

    def place(idx: int, val: int) -> None:
        prev = S.get(idx)
        if prev is not None and prev != val:
            raise CodecError(f"S[{idx}] conflict: was {prev:#04x}, set {val:#04x}")
        S[idx] = val

    k = 0
    last_iter_cost = 1
    while k < N:
        cur = D[k]
        b2_idx = cost_sum
        b1_idx = cost_sum + 1
        b0_idx = cost_sum + 2

        if cur == ESC:
            place(b2_idx, ESC)
            place(b1_idx, ESC)
            cost_sum += 2
            last_iter_cost = 2
            k += 1
            continue

        run_len = 1
        while k + run_len < N and D[k + run_len] == cur and run_len < 256:
            run_len += 1

        if run_len >= 3:
            count_byte = run_len - 2
            place(b2_idx, count_byte)
            place(b1_idx, ESC)
            place(b0_idx, cur)
            cost_sum += 3
            last_iter_cost = 3
            k += run_len
            continue

        if k + 1 < N and D[k + 1] == ESC:
            place(b2_idx, cur)
            place(b1_idx, ESC)
            place(b0_idx, ESC)
            cost_sum += 1
            last_iter_cost = 1
            k += 1
            continue

        place(b2_idx, cur)
        cost_sum += 1
        last_iter_cost = 1
        k += 1

    # body_size = cost_sum + 5 - last_iter_cost places the LAST iter's
    # body bytes at body[0..2]. The factor depends on the last iter's
    # path cost: Path A/C → +4, Path B → +3, Path D → +2.
    # For tunes ending in a zero run (most), Path D wins and body_size
    # = cost_sum + 2 — matching defMON byte-for-byte. For T11/T17 with
    # isolated non-zero target bytes at $1804/$1807/$1809 (JP-source
    # authoring), this also avoids the in-place collision cascade,
    # because the JP-source bytes land at body offsets read before
    # dest catches up. See [[project-d6c9-low-address-collision]].
    body_size = cost_sum + 5 - last_iter_cost
    body = bytearray(body_size)
    for idx, val in S.items():
        body_pos = body_size - 3 - idx
        if 0 <= body_pos < body_size - 2:
            body[body_pos] = val

    body[body_size - 2] = dest_start & 0xFF
    body[body_size - 1] = dest_hi_byte

    _verify_inplace_decode(body, dest_start, target)

    return bytes([LOAD_ADDR & 0xFF, LOAD_ADDR >> 8]) + bytes(body)


def _verify_inplace_decode(
    body_bytes: bytes | bytearray,
    dest_start: int,
    target: bytes | bytearray,
) -> None:
    """Simulate the $D6C9 decoder against a *mutable* body buffer.

    Models the live-C64-RAM scenario where dest writes can overwrite
    source bytes before src reads them. Raises ``CodecError`` if the
    simulated decode does not reproduce ``target``.
    """
    body_start = LOAD_ADDR
    body_end = body_start + len(body_bytes)
    work = bytearray(body_bytes)
    above_body: dict[int, int] = {}

    def read(addr: int) -> int:
        if body_start <= addr < body_end:
            return work[addr - body_start]
        return above_body.get(addr, 0)

    def write_dest(addr: int, val: int) -> None:
        if body_start <= addr < body_end:
            work[addr - body_start] = val
        else:
            above_body[addr] = val

    src = body_end - 1 - 4
    dest = dest_start

    while src >= body_start:
        b0 = read(src + 0)
        b1 = read(src + 1)
        b2 = read(src + 2)
        if b1 != ESC:
            write_dest(dest, b2)
            dest -= 1
            src -= 1
        elif b2 == ESC:
            write_dest(dest, ESC)
            dest -= 1
            src -= 2
        elif b0 == ESC:
            write_dest(dest, b2)
            dest -= 1
            src -= 1
        else:
            count = b2 + 2
            for _ in range(count):
                write_dest(dest, b0)
                dest -= 1
            src -= 3

    target_top = dest_start
    diffs: list[int] = []
    for i, expected in enumerate(target):
        addr = target_top - i
        got = (
            work[addr - body_start]
            if body_start <= addr < body_end
            else above_body.get(addr, 0)
        )
        if got != expected:
            diffs.append(addr)
            if len(diffs) >= 4:
                break
    if diffs:
        raise CodecError(
            f"in-place decode mismatch (likely dest-src collision); "
            f"first {len(diffs)} bad addrs: "
            f"{', '.join(f'${a:04X}' for a in diffs)}"
        )


def encode_ram_block(ram: bytes | bytearray, base_addr: int) -> bytes:
    """Encode a contiguous RAM block ``ram[0]@base_addr ... ram[-1]@base_addr+len-1``.

    The $D6C9 decoder writes top-down, so the target byte list is
    reversed and ``dest_start`` is the HIGHEST address.
    """
    if not ram:
        raise CodecError("empty ram block")
    if not 0x1800 <= base_addr <= 0xFFFF:
        raise CodecError(f"base_addr ${base_addr:04X} out of range")
    top_addr = base_addr + len(ram) - 1
    if top_addr > 0xFFFF:
        raise CodecError(f"block ${base_addr:04X}+{len(ram)} overflows past $FFFF")
    target = bytes(reversed(ram))
    return encode_load_stream(target, dest_start=top_addr)
