"""Per-night clock corrections for MuSCAT2 FITS timestamps.

From late July 2026 the IAC network policy change cut alex2 off from its NTP
server, so MuSCAT2 header times drifted by up to ~100 s between 2026-07-23 and
2026-10-22 (issue #422, report from Fukui-san).

``MUSCAT2_CLOCK_OFFSETS`` holds one average value per night, defined by the
reporter as ``Time_correct - Time_FITS`` in seconds. The correction is
therefore **added** to the recorded time. FITS files are never modified: the
offset is applied to the copy of a light curve staged for a fit.

Nights inside the affected window that have no measured offset are not
guessed. The offset steps by up to ~20 s between listed nights (e.g. 260825,
260922), so interpolation is not safe. Callers get ``0.0`` and can flag the
night with :func:`is_uncorrected_night`.
"""

from __future__ import annotations

from types import MappingProxyType

SECONDS_PER_DAY = 86400.0

# Inclusive YYMMDD bounds of the period without NTP access.
AFFECTED_FIRST_NIGHT = "260723"
AFFECTED_LAST_NIGHT = "261022"

_AFFECTED_INSTRUMENT = "muscat2"

# obsdate (YYMMDD) -> Time_correct - Time_FITS [s]; source: issue #422.
MUSCAT2_CLOCK_OFFSETS = MappingProxyType({
    "260723": -0.1,
    "260804": 0.2,
    "260808": -17.4,
    "260810": -17.6,
    "260811": -17.7,
    "260812": -17.7,
    "260813": -17.7,
    "260814": -17.6,
    "260815": -17.6,
    "260817": -17.7,
    "260818": -18.0,
    "260820": -18.2,
    "260821": -18.4,
    "260822": -18.5,
    "260823": -18.7,
    "260824": -18.8,
    "260825": -39.3,
    "260826": -39.4,
    "260827": -42.0,
    "260902": -48.8,
    "260903": -48.9,
    "260905": -52.9,
    "260907": -52.9,
    "260908": -52.9,
    "260909": -53.0,
    "260910": -53.1,
    "260913": -53.6,
    "260914": -53.4,
    "260915": -53.7,
    "260916": -53.9,
    "260918": -53.9,
    "260919": -54.0,
    "260922": -76.9,
    "261013": -95.6,
    "261015": -96.1,
    "261016": -96.4,
    "261019": -97.5,
    "261021": -98.2,
})


def _in_affected_window(inst: str, date: str) -> bool:
    return inst == _AFFECTED_INSTRUMENT and AFFECTED_FIRST_NIGHT <= date <= AFFECTED_LAST_NIGHT


def clock_offset_seconds(inst: str, date: str) -> float:
    """Seconds to add to the recorded time of *inst* on night *date* (YYMMDD).

    ``0.0`` for every other instrument, for nights outside the window, and for
    unlisted nights inside it (see :func:`is_uncorrected_night`).
    """
    if not _in_affected_window(inst, date):
        return 0.0
    return MUSCAT2_CLOCK_OFFSETS.get(date, 0.0)


def is_uncorrected_night(inst: str, date: str) -> bool:
    """True when the night is inside the affected window but has no measured offset."""
    return _in_affected_window(inst, date) and date not in MUSCAT2_CLOCK_OFFSETS
