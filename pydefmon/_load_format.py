"""defMON's ``$D6C9`` LOAD-time codec: RLE decoder and SAVE encoder.

``decode_load_stream`` inverts the on-chip ``$D6C9`` decoder.
``encode_worktune`` ports defMON SAVE's ``$D626`` walk byte-for-byte.
"""

from __future__ import annotations

from pysidtracker import SidError

LOAD_ADDR = 0x1800
ESC = 0xFF


class CodecError(SidError, ValueError):
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

    Termination mirrors $D6D5-$D6E1: SEC + SBC of ``src`` against
    ``src_floor`` (BCS iff ``src >= src_floor``); the $D6E3 fall-through
    allows one extra FF-escape iter, so the walk stays in range then exits.

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


# ---- encode (defMON SAVE walk $CE81/$D626) -----------------------------


def _save_emit_run(v: int, run: int, out: bytearray) -> None:
    """Emit one ``$D626`` run of value ``v`` repeated ``run`` times.

    Runs 1-2 emit ``v`` (doubled if ``v == $FF``); run 2 also repeats that
    pair, mirroring the run-2 path falling through the run-1 path. Runs of
    3+ emit ``v`` (doubled if ``$FF``), then ``$FF`` and ``run - 2``.
    """
    if run >= 3:
        out.append(v)
        if v == ESC:
            out.append(v)
        out.append(ESC)
        out.append(run - 2)
        return
    pairs = 2 if run == 2 else 1
    for _ in range(pairs):
        out.append(v)
        if v == ESC:
            out.append(v)


def encode_worktune(
    src: bytes | bytearray, top: int, *, base: int = LOAD_ADDR
) -> bytes:
    """Encode the save-prepared ``$1800..top`` region as a defMON ``.prg``.

    Faithful port of defMON SAVE (the ``$D626`` walk): the encoder's exact
    run/escape choices, then the trailer encoding ``top``. ``src`` must be
    ``top - base + 1`` bytes; an unmodified load round-trips byte-identical.
    """
    if not (base <= top <= 0xFFFF):
        raise CodecError(f"top ${top:04X} out of range (base ${base:04X})")
    expect = top - base + 1
    if len(src) != expect:
        raise CodecError(
            f"src must be {expect} bytes (${base:04X}..${top:04X}), got {len(src)}"
        )

    body = bytearray()
    n = len(src)
    i = 0
    while i < n:
        v = src[i]
        run = 1
        while i + run < n and src[i + run] == v and run < 256:
            run += 1
        _save_emit_run(v, run, body)
        i += run
    body.append(top & 0xFF)
    body.append(((top >> 8) - 0x18) & 0xFF)

    _verify_decode_worktune(bytes(body), top, bytes(src), base)
    return bytes([base & 0xFF, base >> 8]) + bytes(body)


def _verify_decode_worktune(body: bytes, top: int, src: bytes, base: int) -> None:
    """Round-trip guard: the ``$D6C9`` decoder must reconstruct ``src``."""
    src_end = base + len(body) - 1 - 4
    try:
        writes, _ = decode_load_stream(
            body, src_end_addr=src_end, src_floor=base, dest_start=top
        )
    except CodecError as exc:
        raise CodecError(f"encode_worktune failed self-decode: {exc}") from exc
    for i, val in enumerate(src):
        got = writes.get(base + i)
        if got != val:
            raise CodecError(
                f"encode_worktune round-trip mismatch at ${base + i:04X}: "
                f"want {val:02X}, got {got if got is None else format(got, '02X')}"
            )
