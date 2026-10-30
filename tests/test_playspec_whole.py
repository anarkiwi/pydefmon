"""``specs/defmon.play`` as the play routine, against the replay's own 6502.

A :class:`~pydefmon.DefmonPlayer` runs the whole spec for every play call of
a tune's whole song (:func:`pysidtracker.song_frames`; the start song, else the subtune); after each call its register
writes and the whole 64 KiB, outside the stack page, must equal a plain run of the replay.
"""

import pytest

from pysidtracker import song_frames
from pysidtracker.testing import TuneFetchError, resolve_tune

from pydefmon import DefmonPlayer

from tests._support import assert_same_every_call, replay_player
from tests.test_oracle_hvsc import SUBTUNES, TUNES

SONGS = [(rel, None) for rel in TUNES.values()] + [
    tuple(entry) for entry in SUBTUNES.values()
]


@pytest.mark.parametrize("relpath, song", SONGS)
def test_whole_spec_equals_the_replay_every_call_of_the_whole_song(relpath, song):
    path = resolve_tune(relpath)
    if path is None:
        pytest.skip(f"tune {relpath} unavailable (offline, not cached)")
    data = path.read_bytes()
    subtune = None if song is None else song - 1
    player = DefmonPlayer(data, subtune=subtune)
    assert player.spec_backed
    assert_same_every_call(
        replay_player(data, subtune=subtune), player, song_frames(data, song)
    )


@pytest.mark.oracle
@pytest.mark.parametrize("relpath, song", SONGS)
def test_every_row_the_spec_reads_parses_as_a_sidtab_row(relpath, song):
    path = resolve_tune(relpath)
    if path is None:
        raise TuneFetchError(f"tune {relpath} unavailable (offline, not cached)")
    data = path.read_bytes()
    player = DefmonPlayer(
        data, subtune=None if song is None else song - 1, validate=True
    )
    for _ in range(song_frames(data, song)):
        player.play_frame()
    assert player.validator.results
    assert player.validator.failures() == {}
