"""``specs/defmon.play`` as the play routine, against the replay's own 6502.

A :class:`~pydefmon.DefmonPlayer` runs the whole spec for every play call of
a tune's whole song (:func:`pysidtracker.song_frames`; the start song, else the subtune); after each call its register
writes and the whole 64 KiB, outside the stack page, must equal a plain run of the replay.
"""

import pytest

from pysidtracker import song_frames
from pysidtracker.testing import TuneFetchError, resolve_tune

from pydefmon import DefmonPlayer
from pydefmon._spec_layout import defmon_binding, entry_base, player_bases

from tests._support import (
    aligned_modulo_missed_irqs,
    assert_same_every_call,
    every_call_player,
    replay_player,
)
from tests.test_oracle_hvsc import SUBTUNES, TUNES

# Replays that reach the player through their own wrapper, the reduced entry at the
# player's start plus six, the band called from outside, or a relocated row pointer.
WRAPPED = (
    "MUSICIANS/F/F7sus4/NGC1277_tune_1.sid",
    "MUSICIANS/F/F7sus4/NGC1277_tune_2.sid",
    "MUSICIANS/F/F7sus4/NGC1277_tune_3.sid",
    "MUSICIANS/F/F7sus4/Timewarp_Spheres_2SID.sid",
    "MUSICIANS/G/Goto80/Skan0r.sid",
    "MUSICIANS/G/Goto80/Svenska_Narkotika_3.sid",
    "MUSICIANS/N/NecroPolo/Anticitizen_64.sid",
    "MUSICIANS/F/F7sus4/Triangle_My_Amelie.sid",
)

SONGS = [(rel, None) for rel in (*TUNES.values(), *WRAPPED)] + [
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
    frames = song_frames(data, song)
    assert_same_every_call(every_call_player(data, subtune=subtune), player, frames)


@pytest.mark.parametrize("relpath, song", SONGS)
def test_the_replay_differs_only_by_the_calls_it_misses(relpath, song):
    path = resolve_tune(relpath)
    if path is None:
        pytest.skip(f"tune {relpath} unavailable (offline, not cached)")
    data = path.read_bytes()
    subtune = None if song is None else song - 1
    frames = song_frames(data, song)
    real = replay_player(data, subtune=subtune).render_grid(frames)
    ours = DefmonPlayer(data, subtune=subtune).render_grid(frames + 8)
    assert aligned_modulo_missed_irqs(real, ours) is not None


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


def test_a_stereo_replay_runs_a_spec_per_copy_of_the_player():
    path = resolve_tune("MUSICIANS/F/F7sus4/Timewarp_Spheres_2SID.sid")
    if path is None:
        pytest.skip("tune unavailable (offline, not cached)")
    player = DefmonPlayer(path.read_bytes())
    bases = player_bases(player.view())
    assert len(bases) == 2
    assert {entry_base(player.view(), e) for e in player._entries()} == set(bases)
    chips = {defmon_binding(player.view(), base)["sid"] for base in bases}
    assert chips == {0xD400, 0xD500}
