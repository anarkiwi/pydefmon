"""Command line interface: tune info, SID register logs, and WAV render.

``pydefmon info TUNE.sid`` prints the replay's metadata; ``pydefmon reglog
TUNE.sid OUT`` writes a shared ``py*`` register log; ``pydefmon wav TUNE.sid
OUT.wav`` renders through an emulated SID. All three run the tune's own replay
(:class:`~pydefmon.DefmonPlayer`) at its play-routine cadence for ``--seconds``
of playback, over the PSID/RSID replay HVSC ships defMON tunes as.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pysidtracker import (
    PAL_CLOCK_HZ,
    SidImage,
    add_reglog_command,
    add_wav_command,
    print_info,
    register_writes_from_player,
    render_player_wav,
    write_reglog,
)
from pysidtracker.cli import run_cli

from pydefmon.defmon import DefmonError
from pydefmon.defmon_player import DefmonPlayer

_SONG_HELP = "defMON PSID/RSID replay"


def _info(args: argparse.Namespace) -> None:
    header = SidImage.from_bytes(Path(args.song).read_bytes()).header
    if header is None:
        raise DefmonError(f"{args.song}: not a PSID/RSID replay (no header)")
    init = header.init_address or header.real_load_address
    print_info(
        header.name,
        header.author,
        header.released,
        header.real_load_address,
        init,
        header.play_address or init,
    )


def _reglog(args: argparse.Namespace) -> None:
    player = DefmonPlayer(args.song)
    cpf = player.cycles_per_frame
    frames = max(1, round(args.seconds * PAL_CLOCK_HZ / cpf))
    writes = register_writes_from_player(
        player, max_frames=frames, cycles_per_frame=cpf
    )
    write_reglog(writes, args.output)
    print(f"wrote {args.output}")


def _wav(args: argparse.Namespace) -> None:
    render_player_wav(
        DefmonPlayer(args.song), args.output, seconds=args.seconds, model=args.model
    )
    print(f"wrote {args.output}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pydefmon", description="defMON tune tools")
    commands = parser.add_subparsers(dest="command", required=True)

    info = commands.add_parser("info", help="print replay metadata")
    info.add_argument("song", help=_SONG_HELP)
    info.set_defaults(func=_info)

    add_reglog_command(commands, _reglog, song_help=_SONG_HELP)
    add_wav_command(commands, _wav, song_help=_SONG_HELP)
    return parser


def main(argv: "list[str] | None" = None) -> int:
    """CLI entry point; returns a process exit code."""
    return run_cli(_parser, DefmonError, argv)


if __name__ == "__main__":
    sys.exit(main())
