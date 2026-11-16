"""Time-series noise model: the precision a star and a set of settings reach.

Builds on the archive-calibrated :class:`muscat_db.exposure_model.BandModel`,
which gives the star's count rate and the image size at a given defocus. One
frame's fractional noise is the quadrature sum of

* target photon noise;
* sky, read noise and dark current inside the photometric aperture, whose
  area follows the FWHM and so grows with defocus;
* the comparison ensemble's own photon, sky, read and dark noise;
* scintillation, from Young's (1967) relation in the form of Osborn et al.
  (2015, MNRAS 452, 1707, eq. 7) with their empirical correction.

The figure of merit is that noise averaged over a fixed time bin, counting
each frame's readout overhead, so it measures precision per unit time. It
depends on the star and the settings only, not on any planet.

Everything here is a pure function of its arguments: read noise, sky rate,
telescope diameter and site altitude are inputs, so the caller decides where
they come from (FITS header first, then the prose2 telescope file).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, fields

from muscat_db.exposure_model import BandModel, fwhm_arcsec
from muscat_db.exposure_model_defaults import READOUT_OVERHEAD_S

# Osborn et al. 2015 eq. 7: variance for C_Y = 1, D = 1 m, t = 1 s at zenith.
SCINTILLATION_VARIANCE_COEFF = 10e-6
# Scale height of the turbulence in Young's relation (m).
TURBULENCE_SCALE_HEIGHT_M = 8000.0
# Osborn's empirical correction to Young's relation. Site medians span 1.30
# (La Palma) to 1.67 (San Pedro Martir); 1.5 is their recommended value where
# no turbulence profile exists.
OSBORN_CY_MEDIAN = 1.5

# Aperture radius in units of the image FWHM. The aperture is taken to enclose
# all of the star's light; phase 3's excess-noise factor absorbs what it loses.
APERTURE_RADIUS_FWHM = 1.5

DEFAULT_BIN_S = 600.0


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _positive(value: float, name: str) -> None:
    _require(math.isfinite(value) and value > 0, f"{name} must be positive and finite, got {value!r}")


def _non_negative(value: float, name: str) -> None:
    _require(math.isfinite(value) and value >= 0, f"{name} must be non-negative and finite, got {value!r}")


def scintillation(diameter_m: float, exptime_s: float, airmass: float, altitude_m: float,
                  c_y: float = OSBORN_CY_MEDIAN) -> float:
    """Fractional scintillation rms of one exposure (Osborn et al. 2015, eq. 7).

    ``c_y=1`` gives Young's uncorrected relation.
    """
    _positive(diameter_m, "diameter_m")
    _positive(exptime_s, "exptime_s")
    _positive(c_y, "c_y")
    _require(math.isfinite(airmass) and airmass >= 1.0, f"airmass must be >= 1, got {airmass!r}")
    _require(math.isfinite(altitude_m), f"altitude_m must be finite, got {altitude_m!r}")
    variance = (SCINTILLATION_VARIANCE_COEFF * c_y ** 2 * diameter_m ** (-4.0 / 3.0) / exptime_s
                * airmass ** 3 * math.exp(-2.0 * altitude_m / TURBULENCE_SCALE_HEIGHT_M))
    return math.sqrt(variance)


def star_rate(model: BandModel, mag: float, airmass: float, extinction: float) -> float:
    """Electrons per second from a star of ``mag`` in typical transparency."""
    _require(math.isfinite(mag), f"magnitude must be finite, got {mag!r}")
    return 10 ** (-0.4 * (mag + extinction * (airmass - 1.0) - model.zp_median))


def aperture_npix(model: BandModel, defocus_mm: float, seeing: float | None = None) -> float:
    """Pixels inside the photometric aperture (default seeing: the band's median)."""
    fwhm = fwhm_arcsec(model, defocus_mm, seeing if seeing and seeing > 0 else model.seeing_median)
    return math.pi * (APERTURE_RADIUS_FWHM * fwhm / model.pixscale) ** 2


@dataclass(frozen=True)
class NoiseBudget:
    """Fractional rms of each noise term; ``total`` is their quadrature sum."""

    target_photon: float
    sky: float
    read: float
    dark: float
    comparison: float
    scintillation: float

    def terms(self) -> dict[str, float]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @property
    def total(self) -> float:
        return math.sqrt(sum(value ** 2 for value in self.terms().values()))

    @property
    def dominant(self) -> str:
        terms = self.terms()
        return max(terms, key=terms.get)


def noise_budget(model: BandModel, mag: float, exptime_s: float, *, airmass: float, extinction: float,
                 diameter_m: float, altitude_m: float, read_noise_e: float, sky_rate: float,
                 dark_rate: float = 0.0, defocus_mm: float = 0.0, seeing: float | None = None,
                 comp_mags: Sequence[float] = (), c_y: float = OSBORN_CY_MEDIAN) -> NoiseBudget:
    """Noise of one frame's differential flux, as a fraction of the target flux.

    ``sky_rate`` and ``dark_rate`` are e-/s per pixel, ``read_noise_e`` e- per
    pixel. The comparison stars are summed into one ensemble, so equal stars
    lower its noise as 1/sqrt(N) and a bright one dominates it. Scintillation
    is taken as uncorrelated between stars.
    """
    _positive(exptime_s, "exptime_s")
    _non_negative(read_noise_e, "read_noise_e")
    _non_negative(sky_rate, "sky_rate")
    _non_negative(dark_rate, "dark_rate")
    npix = aperture_npix(model, defocus_mm, seeing)
    electrons = star_rate(model, mag, airmass, extinction) * exptime_s
    sky_var = npix * sky_rate * exptime_s
    read_var = npix * read_noise_e ** 2
    dark_var = npix * dark_rate * exptime_s

    comparison, ensemble_weight = 0.0, 0.0
    comps = [star_rate(model, m, airmass, extinction) * exptime_s for m in comp_mags]
    if comps:
        ensemble = sum(comps)
        comparison = math.sqrt(ensemble + len(comps) * (sky_var + read_var + dark_var)) / ensemble
        ensemble_weight = sum(c ** 2 for c in comps) / ensemble ** 2
    scint = scintillation(diameter_m, exptime_s, airmass, altitude_m, c_y)
    return NoiseBudget(
        target_photon=1.0 / math.sqrt(electrons), sky=math.sqrt(sky_var) / electrons,
        read=math.sqrt(read_var) / electrons, dark=math.sqrt(dark_var) / electrons,
        comparison=comparison, scintillation=scint * math.sqrt(1.0 + ensemble_weight),
    )


def binned(budget: NoiseBudget, exptime_s: float, overhead_s: float, bin_s: float = DEFAULT_BIN_S) -> NoiseBudget:
    """A per-frame budget averaged over ``bin_s`` of wall-clock time.

    A bin holds ``bin_s / (exptime_s + overhead_s)`` frames, so readout
    overhead costs precision.
    """
    _positive(exptime_s, "exptime_s")
    _non_negative(overhead_s, "overhead_s")
    _positive(bin_s, "bin_s")
    scale = math.sqrt((exptime_s + overhead_s) / bin_s)
    return NoiseBudget(**{name: value * scale for name, value in budget.terms().items()})


def ppm(fraction: float) -> float:
    return fraction * 1e6


def readout_overhead(instrument: str, mode: str) -> float:
    """Seconds between exposures in one readout mode."""
    try:
        return READOUT_OVERHEAD_S[instrument][mode]
    except KeyError:
        known = ", ".join(sorted(READOUT_OVERHEAD_S.get(instrument, {}))) or "none"
        raise ValueError(f"no readout overhead for {instrument} mode {mode!r} (known modes: {known})") from None
