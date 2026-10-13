"""pydefmon's renderer for the ``pysidtracker.survey`` whole-song HVSC oracle survey.

    PYTHONPATH=scripts python -m pysidtracker.survey run $HVSC tunes.csv \
        --work .hvsc-survey --render oracle_hvsc:render
"""

import numpy as np

from pysidtracker.cadence import playroutine_cadence
from pysidtracker.registers import PAL_CYCLES_PER_FRAME

from pydefmon import DefmonPlayer, DefmonSong

PLAYER = "^defmon$"


def render(data, song, nframes):
    """Render 1-based ``song`` of a defMON ``.sid`` and the replay variant it decoded."""
    tune = DefmonSong.from_sid_bytes(data)
    cadence = playroutine_cadence(data, subtune=song - 1)
    info = {
        "layout": tune.replay_layout,
        "rsid": data[:4] == b"RSID",
        "trigger": cadence.source and cadence.source.value,
        "speed": round(PAL_CYCLES_PER_FRAME / cadence.cycles_per_call),
        "dynamic": cadence.dynamic,
    }
    return (
        np.asarray(DefmonPlayer(data, subtune=song - 1).render_grid(nframes), np.int16),
        info,
    )
