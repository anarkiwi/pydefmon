"""``defmon.play`` holds every player variable as state and runs on the native core.

The state starts from what the replay's init leaves, ``SpecPlayer.state`` and ``set_state``
read and set it by name, the tunes the binder refuses are those with two copies of the
player, and a table whose index can reach a variable is refused.
"""

import pytest

from pysidtracker import song_frames
from pysidtracker.testing import resolve_tune

from pydefmon import DefmonPlayer
from pydefmon.format import defmon_format
from pydefmon.spec import (
    VOICE_STRIDE,
    defmon_binding,
    entry_base,
    player_bases,
    player_places,
    spec_bind,
)

from tests._support import (
    CORPUS_LIST,
    corpus_relpaths,
    replay_state,
    resolve_corpus,
)

FMT = defmon_format()
REFUSED = (CORPUS_LIST.parent / "spec_refused.txt").read_text(encoding="ascii").split()
PLAYED = (
    "MUSICIANS/G/Goto80/Dr_Ghetto.sid",
    "MUSICIANS/D/Demsky_Martin/Minimal.sid",
    "MUSICIANS/I/Ilkke/Glow_Worm.sid",
)


def _player(relpath: str, **kwargs) -> DefmonPlayer:
    path = resolve_tune(relpath)
    if path is None:
        pytest.skip(f"tune {relpath} unavailable (offline, not cached)")
    return DefmonPlayer(path.read_bytes(), **kwargs)


def test_the_tunes_without_a_spec_are_those_with_two_copies_of_the_player():
    assert set(REFUSED) <= set(corpus_relpaths())
    found = resolve_corpus()
    if len(found) < len(corpus_relpaths()):
        pytest.skip("part of the corpus is unavailable (offline, not cached)")
    for relpath, path in found:
        player = DefmonPlayer(path.read_bytes())
        copies = len(player_bases(player.view()))
        assert player.spec_backed == (relpath not in REFUSED), relpath
        assert (copies > 1) == (relpath in REFUSED), relpath


@pytest.mark.parametrize("relpath", PLAYED)
def test_the_state_starts_from_what_the_replays_init_leaves(relpath):
    player = _player(relpath)
    want = replay_state(player.view(), player.entry)
    assert want and all(player.state(key) == value for key, value in want.items())


@pytest.mark.parametrize("relpath", PLAYED)
def test_the_spec_runs_on_the_native_core_without_falling_back(relpath):
    path = resolve_tune(relpath)
    if path is None:
        pytest.skip(f"tune {relpath} unavailable (offline, not cached)")
    data = path.read_bytes()
    player = FMT.spec_player(FMT.parser.parse(data), native=True)
    player.render_grid(song_frames(data))
    # pylint: disable=protected-access
    assert player._fast and player._bails == 0 and player._core is not None


def _quiet(player) -> None:
    """Nothing but the voices' pitch runs: the sidTAB tables halt and the readers wait."""
    for k in range(6):
        player.set_state(f"table[{k}].wait", 0x80)
    for x in range(3):
        player.set_state(f"reader[{x}].counter", 0x3F)
    player.set_state("trigger", 0)


def test_a_halted_sidtab_table_stays_and_a_table_at_zero_applies_its_row():
    player = _player(PLAYED[0])
    _quiet(player)
    player.set_state("table[2].pos", 7)
    player.play_frame()
    assert player.state("table[2].pos") == 7 and player.state("table[2].wait") == 0x80
    player.set_state("table[2].wait", 0)
    player.play_frame()
    assert player.state("table[2].pos") == 8


def test_a_voices_pitch_is_its_note_with_the_detune_a_step_or_a_slide():
    player = _player(PLAYED[1])
    view = player.view()
    places = player_places(view, entry_base(view, player.entry))
    detune = view[places["detune"] + VOICE_STRIDE]

    def pitch(index: int) -> int:
        return view[places["pitch_hi"] + index] << 8 | view[places["pitch_lo"] + index]

    for note in (36, 60):
        _quiet(player)
        player.set_state("voice[1].note", note)
        player.set_state("voice[1].depth", 0)
        player.set_state("voice[1].mode", 0)
        player.play_frame()
        word = pitch(note)
        assert player.state("voice[1].freq") == word & 0xFF00 | (word + detune) & 0xFF
        player.set_state("voice[1].mode", 2)
        player.play_frame()
        step = pitch(note + 2 - 24) - pitch(note + 2 - 25)
        assert player.state("voice[1].freq") == (word + step) & 0xFFFF
        player.set_state("voice[1].mode", 0x84)
        player.set_state("voice[1].slide", 0x0100)
        player.play_frame()
        rate = view[places["rate_hi"] + 8] << 8 | view[places["rate_lo"] + 8]
        slid = (0x0100 + rate) & 0xFFFF
        assert player.state("voice[1].slide") == slid
        assert player.state("voice[1].freq") == (word + slid) & 0xFFFF


def test_a_table_that_reaches_a_player_variable_is_refused():
    player = _player(PLAYED[1])
    view, entry = player.view(), player.entry
    base = entry_base(view, entry)
    places = dict(player_places(view, base, entry))
    assert defmon_binding(view, base, entry, places)["sid"] == places["sid"]
    places["v2"] = places["trigger"] - 3
    with pytest.raises(ValueError, match="reaches the variable"):
        defmon_binding(view, base, entry, places)


def test_a_stereo_replay_is_refused_by_the_binder():
    path = resolve_tune(REFUSED[0])
    if path is None:
        pytest.skip("tune unavailable (offline, not cached)")
    player = DefmonPlayer(path.read_bytes())
    with pytest.raises(ValueError, match="stereo"):
        spec_bind(player.view(), player_bases(player.view())[0] + 3)
