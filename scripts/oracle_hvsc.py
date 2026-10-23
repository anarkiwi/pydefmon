"""pydefmon's renderer for the ``pysidtracker.survey`` whole-song HVSC oracle survey.

    PYTHONPATH=scripts python -m pysidtracker.survey run $HVSC tunes.csv \
        --work .hvsc-survey --render oracle_hvsc:render
"""

import numpy as np

from pysidtracker.cadence import cadence_info

from pydefmon import DefmonPlayer, DefmonSong

PLAYER = "^defmon$"


def render(data, song, nframes):
    """Render 1-based ``song`` of a defMON ``.sid`` and the replay variant it decoded."""
    tune = DefmonSong.from_sid_bytes(data)
    info = {"layout": tune.replay_layout, **cadence_info(data, song)}
    return (
        np.asarray(DefmonPlayer(data, subtune=song - 1).render_grid(nframes), np.int16),
        info,
    )
