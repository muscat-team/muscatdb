"""One-band precision recommendations using measured observing conditions."""
from statistics import median
from typing import Literal

from astropy.io import fits
from pydantic import BaseModel, ConfigDict, Field

from muscat_db import exposure, exposure_model, precision
from muscat_db.cache import register_cache
from muscat_db.instruments import INSTRUMENTS
from muscat_db.lco import _DEFOCUS_LIMIT_MM
from muscat_db.transit_obs import LCO_SITES


class RecommendationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)

    instrument: Literal['muscat3', 'muscat4', 'sinistro', 'qhy600']
    band: Literal['gp', 'rp', 'ip', 'zs']
    mode: str
    site: Literal['ogg', 'coj', 'lsc', 'cpt', 'tfn', 'elp']
    mag: float = Field(ge=-10, le=40)
    comp_mags: list[float] = Field(default_factory=list, max_length=100)
    airmass_min: float = Field(default=1, ge=1, le=3)
    airmass_max: float = Field(default=2, ge=1, le=3)
    read_noise_e: float = Field(ge=0, le=1000)
    sky_rate: float = Field(ge=0, le=1e6)
    dark_rate: float = Field(default=0, ge=0, le=1e6)
    max_cycle_s: float = Field(default=120, gt=0, le=3600)
    bin_s: float = Field(default=600, gt=0, le=86400)
    sat_frac: float = Field(default=.5, gt=0, le=1)


def recommend(request: RecommendationRequest) -> dict:
    inst = request.instrument
    fixed_site = {'muscat3': 'ogg', 'muscat4': 'coj'}.get(inst)
    if fixed_site and request.site != fixed_site:
        raise ValueError(f'{inst} is at {fixed_site}; select that site')
    overhead = precision.readout_overhead(inst, request.mode)
    model_mode = exposure_model.mode_key(inst, request.mode)
    conn = exposure._conn()
    try:
        model = exposure_model.models_for(conn, inst).get((request.band, model_mode))
    finally:
        conn.close()
    if model is None:
        raise ValueError('No calibrated model for this band and readout mode')
    max_focus = min(model.max_defocus_mm, _DEFOCUS_LIMIT_MM[inst])
    result = precision.recommend(
        model, request.mag, comp_mags=request.comp_mags,
        airmass_range=(request.airmass_min, request.airmass_max),
        extinction=exposure.EXTINCTION[request.band],
        diameter_m=exposure.INSTRUMENT_PARAMS[inst]['aperture_m'],
        altitude_m=LCO_SITES[request.site][2], read_noise_e=request.read_noise_e,
        sky_rate=request.sky_rate, dark_rate=request.dark_rate, overhead_s=overhead,
        max_cycle_s=request.max_cycle_s, bin_s=request.bin_s, sat_frac=request.sat_frac,
        max_defocus_mm=max_focus,
    )
    notes = ['Model estimate, not calibrated against achieved light-curve precision. '
             'Uses archive seeing, a 1.5 FWHM aperture and scintillation coefficient 1.5; '
             'excludes blends and correlated noise.']
    if fixed_site:
        notes.append(f'Calculated for {request.band}. On MuSCAT3 and MuSCAT4, '
                     'the telescope applies this defocus to all four bands.')
    if not max_focus:
        notes.append('In-focus only: no recorded calibrated defocus range. Recalibrate to enable defocus search.')
    if not request.comp_mags:
        notes.append('No comparison stars: target-only precision; differential precision will be worse.')
    return {'ok': True, **result, 'inputs': request.model_dump(), 'overhead_s': overhead,
            'max_defocus_mm': max_focus, 'model': model.as_dict(), 'notes': notes}


@register_cache(ttl=3600, maxsize=128)
def _archive_conditions(instrument: str, band: str, mode: str, site: str, db_path: str, root: str) -> dict:
    """Read up to 200 recent headers; DB/root arguments isolate cached data sets."""
    conn = exposure._conn()
    try:
        frames = conn.execute(
            "SELECT obsdate, filename FROM frames WHERE instrument=? AND filter=? AND read_mode=? "
            "AND substr(filename, 1, 3)=? ORDER BY obsdate DESC, filename DESC LIMIT 200",
            (instrument, band, mode, site),
        ).fetchall()
    finally:
        conn.close()
    groups = {}
    for date, filename in frames:
        path = exposure._fits_exists(instrument, date, filename)
        if path is None:
            continue
        try:
            with fits.open(path) as hdul:
                if 'SCI' not in hdul:
                    continue
                header = hdul['SCI'].header
        except (OSError, ValueError):
            continue
        key = (header.get('SITEID'), header.get('FILTER'), header.get('CONFMODE'))
        values = groups.setdefault(key, {k: [] for k in ('sky_rate', 'read_noise_e', 'dark_rate')})
        # BANZAI's L1MEDIAN is counts; RDNOISE and DARKCURR are already electrons.
        sky, gain, exp = (exposure_model._float(header, k) for k in ('L1MEDIAN', 'GAIN', 'EXPTIME'))
        if sky is not None and gain is not None and exp is not None and gain > 0 and exp > 0:
            rate = sky * gain / exp
            if 0 <= rate <= 1e6:
                values['sky_rate'].append(rate)
        for field, keyword, limit in [('read_noise_e', 'RDNOISE', 1000), ('dark_rate', 'DARKCURR', 1e6)]:
            value = exposure_model._float(header, keyword)
            if value is not None and 0 <= value <= limit:
                values[field].append(value)
    return groups


def recommendation_defaults(instrument: str, band: str, mode: str, site: str) -> dict:
    precision.readout_overhead(instrument, mode)
    groups = _archive_conditions(instrument, band, mode, site, exposure.db_path(), INSTRUMENTS[instrument].data_dir)
    samples = groups.get((site, band, mode), {})
    keys = ('sky_rate', 'read_noise_e', 'dark_rate')
    return {'ok': True,
            'values': {k: median(samples[k]) if samples.get(k) else None for k in keys},
            'counts': {k: len(samples.get(k, [])) for k in keys}}
