"""Download + extract defMON tune fixtures from the upstream csdb.dk
release.

These files are not redistributed with pydefmon. Tests look for them
under the cache directory (``./build/fixtures/`` by default, override
with ``PYDEFMON_FIXTURES_DIR``) and ``skipTest`` if absent.

Run::

    python -m tools.fetch_fixtures               # download + extract
    python -m tools.fetch_fixtures --d64 PATH    # extract from local .d64

The default download is the official defMON V20201008 release on csdb
(https://csdb.dk/release/?id=196474), which ships as a .zip containing
two .d64 disk images. We only extract PRG files from
``defmon-withtunes.d64``; the editor disk is ignored.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_URL = "https://csdb.dk/getinternalfile.php/204680/defmon-20201008.zip"
DEFAULT_CACHE_DIR = Path(os.environ.get("PYDEFMON_FIXTURES_DIR", "build/fixtures"))
WITHTUNES_D64 = "defmon-withtunes.d64"

# d64 geometry: 35 tracks, varying sectors per track. Index 0 unused;
# tracks are 1-based.
SECTORS_PER_TRACK = [0] + [21] * 17 + [19] * 7 + [18] * 6 + [17] * 5
SECTOR_SIZE = 256
D64_SIZE = sum(SECTORS_PER_TRACK[1:]) * SECTOR_SIZE  # 174848
DIR_TRACK = 18
DIR_SECTOR = 1
FILETYPE_PRG = 0x82


def _ts_offset(track: int, sector: int) -> int:
    if not 1 <= track <= 35:
        raise ValueError(f"track out of range: {track}")
    if not 0 <= sector < SECTORS_PER_TRACK[track]:
        raise ValueError(f"sector out of range for track {track}: {sector}")
    return sum(SECTORS_PER_TRACK[1:track]) * SECTOR_SIZE + sector * SECTOR_SIZE


def _walk_directory(d64: bytes):
    """Yield ``(name, start_track, start_sector)`` for every PRG entry."""
    track, sector = DIR_TRACK, DIR_SECTOR
    seen = set()
    while (track, sector) not in seen:
        seen.add((track, sector))
        off = _ts_offset(track, sector)
        block = d64[off : off + SECTOR_SIZE]
        next_track, next_sector = block[0], block[1]
        for slot in range(8):
            entry = block[2 + slot * 32 : 2 + slot * 32 + 30]
            if not entry or entry[0] != FILETYPE_PRG:
                continue
            start_t, start_s = entry[1], entry[2]
            name = entry[3:19].rstrip(b"\xa0").decode("latin-1", errors="replace")
            yield name, start_t, start_s
        if next_track == 0:
            break
        track, sector = next_track, next_sector


def _extract_file(d64: bytes, start_track: int, start_sector: int) -> bytes:
    """Follow the T/S chain starting at (start_track, start_sector) and
    return the concatenated payload bytes."""
    out = bytearray()
    t, s = start_track, start_sector
    visited = set()
    while True:
        if (t, s) in visited:
            raise RuntimeError(f"sector chain loop at T{t}S{s}")
        visited.add((t, s))
        off = _ts_offset(t, s)
        block = d64[off : off + SECTOR_SIZE]
        next_t, next_s = block[0], block[1]
        if next_t == 0:
            # next_s is the index of the last used byte in this sector
            # (so payload is bytes 2..next_s+1, total next_s - 1 bytes).
            out.extend(block[2 : next_s + 1])
            break
        out.extend(block[2:SECTOR_SIZE])
        t, s = next_t, next_s
    return bytes(out)


def _sanitize(name: str) -> str:
    name = name.lstrip(".").strip().lower()
    keep = []
    for ch in name:
        if ch.isalnum():
            keep.append(ch)
        elif ch in " -_":
            keep.append("_")
    out = "".join(keep).strip("_")
    while "__" in out:
        out = out.replace("__", "_")
    return out or "tune"


def extract_d64(d64_bytes: bytes, out_dir: Path) -> list[Path]:
    """Extract every PRG in ``d64_bytes`` to ``out_dir``. Returns the
    list of written paths."""
    if len(d64_bytes) < D64_SIZE:
        raise ValueError(
            f"d64 too short: {len(d64_bytes)} bytes (expected >= {D64_SIZE})"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, start_t, start_s in _walk_directory(d64_bytes):
        payload = _extract_file(d64_bytes, start_t, start_s)
        fname = f"{_sanitize(name)}.prg"
        path = out_dir / fname
        path.write_bytes(payload)
        written.append(path)
    return written


def _download(url: str) -> bytes:
    print(f"fetching {url}", file=sys.stderr)
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read()


def _d64_from_zip(blob: bytes, member_name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        candidates = [n for n in zf.namelist() if n.endswith(member_name)]
        if not candidates:
            raise RuntimeError(
                f"member {member_name!r} not found in zip; "
                f"contents: {zf.namelist()}"
            )
        return zf.read(candidates[0])


def fetch(out_dir: Path, url: str = DEFAULT_URL, keep_d64: bool = False) -> list[Path]:
    """Download the csdb release, extract the with-tunes d64, and
    write every PRG to ``out_dir``. When ``keep_d64`` is true, also
    write the raw d64 next to the PRGs (needed by the live-VICE
    integration test, which mounts the d64 into asid-vice)."""
    blob = _download(url)
    d64 = _d64_from_zip(blob, WITHTUNES_D64) if url.endswith(".zip") else blob
    written = extract_d64(d64, out_dir)
    if keep_d64:
        d64_path = out_dir / WITHTUNES_D64
        d64_path.write_bytes(d64)
        written.append(d64_path)
    return written


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"upstream archive URL (default: {DEFAULT_URL})",
    )
    ap.add_argument(
        "--d64",
        type=Path,
        help="extract from this local .d64 instead of downloading",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help=f"destination dir (default: {DEFAULT_CACHE_DIR}, override "
        "with PYDEFMON_FIXTURES_DIR)",
    )
    ap.add_argument(
        "--keep-d64",
        action="store_true",
        help=f"also write the raw {WITHTUNES_D64} to --out-dir "
        "(needed by the live-VICE integration test)",
    )
    args = ap.parse_args(argv)

    if args.d64 is not None:
        paths = extract_d64(args.d64.read_bytes(), args.out_dir)
        if args.keep_d64:
            dest = args.out_dir / WITHTUNES_D64
            dest.write_bytes(args.d64.read_bytes())
            paths.append(dest)
    else:
        paths = fetch(args.out_dir, args.url, keep_d64=args.keep_d64)

    print(f"wrote {len(paths)} files to {args.out_dir}:", file=sys.stderr)
    for p in paths:
        print(f"  {p.name} ({p.stat().st_size} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
