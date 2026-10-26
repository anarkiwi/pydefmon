"""pydefmon's renderer for the ``pysidtracker.survey`` whole-song HVSC oracle survey.

    PYTHONPATH=scripts python -m pysidtracker.survey run $HVSC tunes.csv \
        --work .hvsc-survey --render oracle_hvsc:render
"""

import numpy as np

from pydefmon import DefmonPlayer, DefmonSong
from pydefmon.format import defmon_format

PLAYER = "^defmon$"


def render(data, song, nframes):
    """Render 1-based ``song`` of a defMON ``.sid`` and its variant facets."""
    tune = DefmonSong.from_sid_bytes(data)
    info = defmon_format().info(tune, data, song)
    return (
        np.asarray(DefmonPlayer(data, subtune=song - 1).render_grid(nframes), np.int16),
        info,
    )
