"""Test-only helpers. Not part of the public package."""

import hashlib
import io
import os
import struct
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

from pysidtracker.testing import TuneFetchError, fetch_prgs, fetch_tune

# A minimal but *runnable* PSID replay: real 6502 code so the DefmonPlayer jennings
# path (init + play + snapshot) is exercised offline, with no copyrighted HVSC
# material. init seeds volume ($D418=$0F) and V0 control ($D404=$21); play
# increments V0 freq-lo ($D400) every call, giving a deterministic grid.
_SYNTH_LOAD = 0x1000
_SYNTH_PLAY_OFF = 0x20
_SYNTH_INIT = bytes(
    [0xA9, 0x0F, 0x8D, 0x18, 0xD4, 0xA9, 0x21, 0x8D, 0x04, 0xD4, 0x60]
)  # LDA #$0F STA $D418  LDA #$21 STA $D404  RTS
_SYNTH_PLAY = bytes([0xEE, 0x00, 0xD4, 0x60])  # INC $D400  RTS


def synthetic_replay() -> bytes:
    """Return a runnable single-voice PSID replay (see module comment)."""
    code = bytearray(0x40)
    code[0 : len(_SYNTH_INIT)] = _SYNTH_INIT
    code[_SYNTH_PLAY_OFF : _SYNTH_PLAY_OFF + len(_SYNTH_PLAY)] = _SYNTH_PLAY
    header = bytearray(0x7C)
    header[0:4] = b"PSID"
    struct.pack_into(">H", header, 0x04, 2)  # version
    struct.pack_into(">H", header, 0x06, 0x7C)  # data offset
    struct.pack_into(">H", header, 0x08, _SYNTH_LOAD)  # load address
    struct.pack_into(">H", header, 0x0A, _SYNTH_LOAD)  # init address
    struct.pack_into(">H", header, 0x0C, _SYNTH_LOAD + _SYNTH_PLAY_OFF)  # play
    struct.pack_into(">H", header, 0x0E, 1)  # songs
    struct.pack_into(">H", header, 0x10, 1)  # start song
    return bytes(header + code)


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES_DIR = Path(
    os.environ.get("PYDEFMON_FIXTURES_DIR", REPO_ROOT / "build" / "fixtures")
)

# Gitignored runtime cache for HVSC tunes fetched by the corpus tests. HVSC
# tunes are copyright works and are NEVER committed; CI caches this dir with
# actions/cache keyed on the corpus list + this file's hash.
TUNECACHE_DIR = Path(
    os.environ.get("PYDEFMON_TUNECACHE", REPO_ROOT / "build" / "tunecache")
)
# Public HVSC mirror serving the C64Music tree over HTTPS.
HVSC_MIRROR = os.environ.get(
    "PYDEFMON_HVSC_MIRROR", "https://hvsc.brona.dk/HVSC/C64Music"
)
# The curated list of DefMon .sid replays (relative to C64Music) the corpus
# tests exercise. Committed (paths only, no tune bytes); drives both the CI
# cache key and the fetch set.
CORPUS_LIST = Path(__file__).resolve().parent / "defmon_corpus.txt"


def corpus_relpaths() -> List[str]:
    """Return the committed list of DefMon ``.sid`` relative paths."""
    if not CORPUS_LIST.exists():
        return []
    return [
        line.strip()
        for line in CORPUS_LIST.read_text(encoding="ascii").splitlines()
        if line.strip()
    ]


def resolve_tune(relpath: str) -> Optional[Path]:
    """Resolve one HVSC tune (relative to ``C64Music``) to a local file.

    Prefers the local ``$HVSC`` tree; otherwise serves it from the gitignored
    tunecache, fetching from :data:`HVSC_MIRROR` (with retries) on a cache miss
    via :func:`pysidtracker.testing.fetch_tune`. Returns ``None`` only if the
    tune is genuinely unreachable (offline runner) -- callers skip that
    individual tune.
    """
    root = hvsc_root()
    if root is not None:
        local = root / relpath
        if local.is_file():
            return local
    try:
        return fetch_tune(relpath, cache_dir=TUNECACHE_DIR, mirror=HVSC_MIRROR)
    except TuneFetchError:
        return None


def resolve_corpus() -> List[Tuple[str, Path]]:
    """Resolve every listed DefMon tune to a local file (fetching + caching
    as needed). Unreachable tunes are omitted (offline runner)."""
    out: List[Tuple[str, Path]] = []
    for rel in corpus_relpaths():
        path = resolve_tune(rel)
        if path is not None:
            out.append((rel, path))
    return out


def hvsc_root() -> Optional[Path]:
    """Return the local HVSC ``C64Music`` tree, or ``None`` if unavailable.

    Honours ``$HVSC`` (or ``$PYDEFMON_HVSC``). HVSC tunes are copyright
    works and are never redistributed with pydefmon; corpus tests that need
    them ``skipTest`` when this returns ``None``.
    """
    for env in ("HVSC", "PYDEFMON_HVSC"):
        val = os.environ.get(env)
        if val and Path(val).is_dir():
            return Path(val)
    return None


def find_defmon_sids(root: Path) -> List[Path]:
    """Return every DefMon ``.sid`` replay under ``root`` (by player signature).

    Mirrors what sidid identifies as ``DefMon``: it scans for defMON's player
    SID-write-band signature in each PSID/RSID image.
    """
    from pydefmon._sid_format import SIGNATURE

    anchor = bytes(SIGNATURE[4:10])  # fixed (non-wildcard) run for prefilter

    def has_sig(buf: bytes) -> bool:
        start = 0
        while True:
            j = buf.find(anchor, start)
            if j < 0:
                return False
            a = j - 4
            if a >= 0 and all(
                w is None or (a + i < len(buf) and buf[a + i] == w)
                for i, w in enumerate(SIGNATURE)
            ):
                return True
            start = j + 1

    out: List[Path] = []
    for path in root.rglob("*.sid"):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if data[:4] in (b"PSID", b"RSID") and has_sig(data):
            out.append(path)
    return sorted(out)


# The official defMON V20201008 release on csdb (https://csdb.dk/release/?id=196474),
# a .zip of two .d64 disk images. The editor .prg workfiles it ships are the only
# fixtures that exercise the $D6C9 LOAD codec and the .prg reader/edit/round-trip
# API; HVSC carries the .sid replays, not these. The tunes are copyright and never
# committed, so they are fetched + extracted into the gitignored cache on demand.
DEFMON_RELEASE_URL = "https://csdb.dk/getinternalfile.php/204680/defmon-20201008.zip"
DEFMON_RELEASE_D64 = "defmon-withtunes.d64"


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
    """Return the path to a defMON ``.prg`` workfile fixture, or raise.

    A local file under ``$PYDEFMON_FIXTURES_DIR`` wins (offline / a warm CI
    cache); otherwise the requested ``.prg`` is extracted from the csdb defMON
    release ``.d64`` (fetched + cached via :func:`pysidtracker.testing.fetch_prgs`)
    on first use. Raises :class:`FileNotFoundError` when the fixture can't be
    produced -- an unreachable release (offline) or a non-``.prg`` name -- so
    callers ``skipTest``.
    """
    dst = FIXTURES_DIR / name
    if dst.exists():
        return dst
    if name.endswith(".prg"):
        try:
            prgs = fetch_prgs(
                DEFMON_RELEASE_URL, cache_dir=FIXTURES_DIR, member=DEFMON_RELEASE_D64
            )
        except TuneFetchError as exc:
            raise FileNotFoundError(f"cannot fetch defMON fixtures: {exc}") from exc
        for f in prgs:
            if f"{_sanitize(f.name)}.prg" == name:
                _atomic_write(dst, f.prg)
                return dst
    raise FileNotFoundError(f"no fixture {name!r} in the defMON release")


DEFMON_EDITOR_D64 = "defmon-20201008.d64"
DEFMON_STATIC_SHA256 = (
    "bc78644c5597a91b86df54aabcc6fbc76ccedf5bf0f3ddc63ac475dc6527b329"
)


def editor_image_bytes() -> Optional[bytes]:
    """64K defMON V20201008 editor RAM image (packing oracle), or ``None``.

    A local ``FIXTURES_DIR/defmon-static.bin`` wins; otherwise the
    editor PRG is fetched from the release .d64, decrunched, sha256-
    verified, and cached. Offline runners get ``None`` and skip.
    """
    dst = FIXTURES_DIR / "defmon-static.bin"
    if dst.exists():
        data = dst.read_bytes()
        if hashlib.sha256(data).hexdigest() == DEFMON_STATIC_SHA256:
            return data
    try:
        prgs = fetch_prgs(
            DEFMON_RELEASE_URL, cache_dir=FIXTURES_DIR, member=DEFMON_EDITOR_D64
        )
    except TuneFetchError:
        return None
    from pysidtracker import native_decrunch

    for f in prgs:
        if not f.name.lstrip(".").upper().startswith("DEFMON"):
            continue
        image = native_decrunch(f.prg)
        if image is None or len(image.mem) != 0x10000:
            continue
        flat = bytes(image.mem)
        if hashlib.sha256(flat).hexdigest() != DEFMON_STATIC_SHA256:
            continue
        _atomic_write(dst, flat)
        return flat
    return None


# Prehistoric 2008 defMON builds (issue #39), author-distributed oldversions.zip: fetched + cached on demand, never committed.
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


def _fetch_bytes(url: str, retries: int = 4) -> bytes:
    """Download ``url`` with a user-agent and retry/backoff; raises TuneFetchError."""
    req = urllib.request.Request(url, headers={"User-Agent": "pydefmon/tests"})
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise TuneFetchError(f"{url}: not found") from exc
            last_err = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_err = exc
        if attempt + 1 < retries:
            time.sleep(min(2**attempt, 5))
    raise TuneFetchError(f"{url}: unreachable after {retries} attempts ({last_err})")


def prehistoric_56block_image() -> Optional[bytes]:
    """64K decrunched RAM image of the 2008 56-block defMON build, or ``None``.

    Zip / PRG / decrunch sha256-pinned (see :data:`PREHISTORIC_*`) from the
    author's ``oldversions.zip``; cached at
    ``FIXTURES_DIR/defmon-56blocks.dcr.bin``. Offline runners get ``None``.
    """
    dst = FIXTURES_DIR / "defmon-56blocks.dcr.bin"
    if dst.exists():
        data = dst.read_bytes()
        if hashlib.sha256(data).hexdigest() == PREHISTORIC_56_BLOCKS_MEM_SHA256:
            return data
    try:
        blob = _fetch_bytes(PREHISTORIC_ZIP_URL)
    except TuneFetchError:
        return None
    if hashlib.sha256(blob).hexdigest() != PREHISTORIC_ZIP_SHA256:
        raise ValueError("oldversions.zip sha256 mismatch (provenance)")
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        prg = next(
            (
                zf.read(n)
                for n in zf.namelist()
                if n.split("/")[-1] == PREHISTORIC_56_BLOCKS
            ),
            None,
        )
    if prg is None or hashlib.sha256(prg).hexdigest() != PREHISTORIC_56_BLOCKS_SHA256:
        raise ValueError(f"{PREHISTORIC_56_BLOCKS} sha256 mismatch (provenance)")
    from pysidtracker import native_decrunch

    image = native_decrunch(prg)
    if image is None or len(image.mem) != 0x10000:
        raise ValueError(f"{PREHISTORIC_56_BLOCKS} did not decrunch to 64K")
    flat = bytes(image.mem)
    if hashlib.sha256(flat).hexdigest() != PREHISTORIC_56_BLOCKS_MEM_SHA256:
        raise ValueError("decrunched 56-block image sha256 mismatch")
    _atomic_write(dst, flat)
    return flat
