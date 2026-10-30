"""Test-only helpers. Not part of the public package.

Every download (HVSC tunes, the csdb defMON release, the author's 2008 builds)
goes through :mod:`pysidtracker.testing` into its one cache root, ``.tunecache``.
"""

import functools
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional, Tuple

from pysidtracker import native_decrunch
from pysidtracker.asm6502 import assemble
from pysidtracker.header import write_psid
from pysidtracker.testing import (
    TuneFetchError,
    default_cache,
    fetch_archive_member,
    fetch_prgs,
    resolve_tune,
)

_SYNTH_LOAD = 0x1000
_SYNTH_PLAY = 0x1020
_SYNTH_INIT = "LDA #$0F\nSTA $D418\nLDA #$21\nSTA $D404\nRTS"
_SYNTH_STEP = "INC $D400\nRTS"


def synthetic_replay(
    init: str = _SYNTH_INIT, play: str = _SYNTH_STEP, songs: int = 1, start_song=1
) -> bytes:
    """A minimal but *runnable* PSID replay, with no copyrighted HVSC material.

    ``init`` (at ``$1000``) and ``play`` (at ``$1020``) are 6502 source, so the
    DefmonPlayer jennings path (init + play + snapshot) runs offline. By default
    init seeds volume ($D418=$0F) and V0 control ($D404=$21) and play increments
    V0 freq-lo ($D400) every call, giving a deterministic grid.
    """
    image = bytearray(0x40)
    for org, source in ((_SYNTH_LOAD, init), (_SYNTH_PLAY, play)):
        code = assemble(source, org)[0]
        image[org - _SYNTH_LOAD : org - _SYNTH_LOAD + len(code)] = code
    return write_psid(
        load=_SYNTH_LOAD,
        init=_SYNTH_LOAD,
        play=_SYNTH_PLAY,
        image=bytes(image),
        songs=songs,
        start_song=start_song,
    )


# The curated list of DefMon .sid replays (relative to C64Music) the corpus
# tests exercise. Committed (paths only, no tune bytes).
CORPUS_LIST = Path(__file__).resolve().parent / "defmon_corpus.txt"


def corpus_relpaths() -> List[str]:
    """Return the committed list of DefMon ``.sid`` relative paths."""
    return CORPUS_LIST.read_text(encoding="ascii").split()


def resolve_corpus() -> List[Tuple[str, Path]]:
    """Every listed DefMon tune resolved to a local file; unreachable ones omitted."""
    return [(rel, p) for rel in corpus_relpaths() if (p := resolve_tune(rel))]


# The official defMON V20201008 release on csdb (https://csdb.dk/release/?id=196474),
# a .zip of two .d64 disk images. The editor .prg workfiles it ships are the only
# fixtures that exercise the $D6C9 LOAD codec and the .prg reader/edit/round-trip
# API; HVSC carries the .sid replays, not these. The tunes are copyright and never
# committed.
DEFMON_RELEASE_URL = "https://csdb.dk/getinternalfile.php/204680/defmon-20201008.zip"
DEFMON_RELEASE_D64 = "defmon-withtunes.d64"
DEFMON_EDITOR_D64 = "defmon-20201008.d64"
DEFMON_STATIC_SHA256 = (
    "bc78644c5597a91b86df54aabcc6fbc76ccedf5bf0f3ddc63ac475dc6527b329"
)


def _sanitize(name: str) -> str:
    """Map a PETSCII disk filename to a stable lowercase ``[a-z0-9_]`` stem."""
    keep = [c if c.isalnum() else "_" for c in name.lstrip(".").strip().lower()]
    out = "".join(keep).strip("_")
    while "__" in out:
        out = out.replace("__", "_")
    return out or "tune"


def _atomic_write(dst: Path, data: bytes) -> None:
    """Write ``data`` to ``dst`` via a temp file + rename (xdist-safe)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dst.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dst)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def fixture_path(name: str) -> Path:
    """The defMON release's ``name`` workfile (a sanitised ``.prg`` name), or raise.

    The release ``.d64`` is fetched once into the cache's ``disks/``
    (:func:`pysidtracker.testing.fetch_prgs`) and its workfiles extracted beside
    it. Raises :class:`FileNotFoundError` when the release is unreachable
    (offline) or has no such file, so callers ``skipTest``.
    """
    dst = default_cache("disks") / "defmon-withtunes" / name
    if dst.exists():
        return dst
    try:
        prgs = fetch_prgs(DEFMON_RELEASE_URL, member=DEFMON_RELEASE_D64)
    except TuneFetchError as exc:
        raise FileNotFoundError(f"cannot fetch defMON fixtures: {exc}") from exc
    for f in prgs:
        if f"{_sanitize(f.name)}.prg" == name:
            _atomic_write(dst, f.prg)
            return dst
    raise FileNotFoundError(f"no fixture {name!r} in the defMON release")


def _decrunched(name: str, sha256: str, prg) -> Optional[bytes]:
    """The 64K image ``native_decrunch`` unpacks from ``prg()``, verified and cached.

    Cached as ``disks/name`` while its SHA-256 is ``sha256``; ``None`` when
    ``prg()`` returns none (offline). Raises :class:`ValueError` when none of them
    decrunches to ``sha256`` (a decrunch regression, not an offline runner).
    """
    dst = default_cache("disks") / name
    if dst.exists() and hashlib.sha256(dst.read_bytes()).hexdigest() == sha256:
        return dst.read_bytes()
    candidates = prg()
    for data in candidates:
        image = native_decrunch(data)
        flat = bytes(image.mem) if image is not None else b""
        if hashlib.sha256(flat).hexdigest() == sha256:
            _atomic_write(dst, flat)
            return flat
    if candidates:
        raise ValueError(f"no {name} candidate decrunches to SHA-256 {sha256}")
    return None


def _editor_prgs():
    try:
        prgs = fetch_prgs(DEFMON_RELEASE_URL, member=DEFMON_EDITOR_D64)
    except TuneFetchError:
        return []
    return [f.prg for f in prgs if f.name.lstrip(".").upper().startswith("DEFMON")]


@functools.cache
def editor_image_bytes() -> Optional[bytes]:
    """64K defMON V20201008 editor RAM image (packing oracle), or ``None`` offline."""
    return _decrunched("defmon-static.bin", DEFMON_STATIC_SHA256, _editor_prgs)


# Prehistoric 2008 defMON builds (issue #39), the author's oldversions.zip.
PREHISTORIC_ZIP_URL = (
    "https://defmon.vandervecken.com/lib/exe/fetch.php?media=download:oldversions.zip"
)
PREHISTORIC_ZIP_SHA256 = (
    "830cc74b31efceb07b61c0b764e3d6b2798993175846b81176498b50b449c09a"
)
PREHISTORIC_56_BLOCKS = "defmon-56blocks.prg"
PREHISTORIC_56_BLOCKS_SHA256 = (
    "82cfa197e89520363002c61ba415863f5b6ef0782b8446ae907f013c63ed7a7e"
)
# 64K RAM image decrunched from the PRG via native_decrunch; pins reproducibility.
PREHISTORIC_56_BLOCKS_MEM_SHA256 = (
    "4504fbc59a6bc1462fbd834f0a8c536e8c60ee3df2752a52176ad88bc4744a24"
)


def _prehistoric_prg():
    try:
        path = fetch_archive_member(
            PREHISTORIC_ZIP_URL,
            PREHISTORIC_ZIP_SHA256,
            PREHISTORIC_56_BLOCKS,
            PREHISTORIC_56_BLOCKS_SHA256,
            archive_name="oldversions.zip",
        )
    except TuneFetchError:
        return []
    return [path.read_bytes()]


@functools.cache
def prehistoric_56block_image() -> Optional[bytes]:
    """64K decrunched RAM image of the 2008 56-block defMON build, or ``None``.

    The zip and PRG are SHA-256-pinned (:func:`fetch_archive_member` raises
    ``ChecksumMismatch`` on a changed pin) and the decrunched image is checked
    against :data:`PREHISTORIC_56_BLOCKS_MEM_SHA256`.
    """
    return _decrunched(
        "defmon-56blocks.dcr.bin", PREHISTORIC_56_BLOCKS_MEM_SHA256, _prehistoric_prg
    )


def editor_player(song_text: str):
    """A :class:`~pydefmon.DefmonPlayer` of ``song_text`` (the song grammar) on the
    editor's own player; raises ``unittest.SkipTest`` without the editor image.
    """
    # pylint: disable-next=import-outside-toplevel
    from pydefmon import DefmonPlayer, Tune
    from pydefmon._worktune_oracle import (  # pylint: disable=import-outside-toplevel
        editor_replay,
    )

    editor = editor_image_bytes()
    if editor is None:
        raise unittest.SkipTest("defMON editor image unavailable")
    prg = Tune.parse(song_text).to_song().to_bytes()
    return DefmonPlayer(editor_replay(editor, prg))


def editor_grid(song_text: str, frames: int) -> list:
    """``frames`` SID register frames of ``song_text`` (the song grammar) on the
    editor's own player; raises ``unittest.SkipTest`` without the editor image.
    """
    return editor_player(song_text).render_grid(frames)


def replay_player(data: bytes, *, subtune: "int | None" = None):
    """The replay ``data`` on the CPU alone: an :class:`~pysidtracker.EmuPlayer`, the
    reference the playspec-backed :class:`~pydefmon.DefmonPlayer` is held to;
    ``subtune`` is 0-based (default: the start song).
    """
    # pylint: disable=import-outside-toplevel
    from pysidtracker import EmuPlayer, SidImage
    from pysidtracker import registers as reg
    from pysidtracker.cadence import playroutine_cadence

    image = SidImage.from_bytes(data)
    header = image.header
    init = header.init_address or header.real_load_address
    if subtune is None:
        subtune = (header.start_song or 1) - 1
    return EmuPlayer(
        image.image,
        image.load,
        init,
        header.play_address or init,
        subtune,
        illegal_opcodes=True,
        sid_model=reg.sid_model_for_flags(header.flags),
        cycles_per_frame=playroutine_cadence(image, subtune=subtune).cycles_per_call,
    )


def assert_same_every_call(plain, other, frames: int) -> None:
    """Play ``frames`` calls of both players; after each, their register writes and the
    whole 64 KiB outside the stack page must be equal. ``other`` is a
    :class:`~pysidtracker.playspec.SpecPlayer`: it must have run its spec exactly as many
    times as ``plain`` executed the instruction at the spec's entry, and at least once.
    """
    # pylint: disable=protected-access
    runs, hits = [0], [0]
    run, entry, step = other._run, other.entry, plain._mpu.step

    def counting_run(address):
        runs[0] += 1
        run(address)

    def counting_step():
        hits[0] += plain._mpu.pc == entry
        return step()

    other._run, plain._mpu.step = counting_run, counting_step
    for frame in range(frames):
        assert plain.play_frame() == other.play_frame(), f"registers at call {frame}"
        ram, mem = plain._mem, other._mem
        if ram[:0x100] != mem[:0x100] or ram[0x200:] != mem[0x200:]:
            differing = [
                hex(a)
                for a in range(0x10000)
                if not 0x100 <= a < 0x200 and ram[a] != mem[a]
            ]
            raise AssertionError(f"memory at call {frame}: {differing[:8]}")
    assert runs[0] == hits[0] > 0, f"the spec ran {runs[0]} times, the replay {hits[0]}"
