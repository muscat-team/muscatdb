# Exposure magnitude lookup

The exposure page prefers measured Pan-STARRS DR1 / SkyMapper DR2 magnitudes.
If Sesame cannot resolve the name, or those catalogs provide no magnitudes,
it asks ExoFOP for the target's coordinates and TIC ID, then queries that exact
ID at MAST. ExoFOP only covers TESS targets; other names still rely on Sesame.
The existing name normalization and catalog lookup remain the first choice.
If TIC returns no usable measurements (or cannot be reached), query SDSS DR17
through its own SkyServer service before considering a Gaia estimate. SDSS also
works for non-TIC names when Sesame resolved coordinates.

Measured TIC griz values take precedence over estimates, including partial
results. Missing bands are cleared from the form, so a previous target's
magnitudes cannot survive a new successful lookup. The source and reason for
an empirical estimate are saved with the values across navigation. Manual
changes are annotated while retaining the original provenance. All displayed values have
at most six decimal places.

## Independent measured SDSS photometry

SDSS queries use a three-arcsecond cone and request at most two primary, clean
stellar detections (`type=6`, `clean=1`). Multiple matches are rejected. The
returned PSF magnitudes are checked band by band: detection must be BINNED1,
and SATURATED, EDGE, NOPROFILE, PSF_FLUX_INTERP and BAD_COUNTS_ERROR flags are
rejected. This is necessary because the catalog's `clean` flag primarily
checks r-band quality. See the [SDSS DR17 flag recommendations](https://www.sdss4.org/dr17/algorithms/photo_flags_recommend/).
Missing, invalid and sentinel magnitudes are ignored. The direct SkyServer
request does not depend on CDS or MAST and uses the same timeout, concurrency
limit and bounded hourly cache as the other fallbacks.

## Gaia fallback

When none of the measured catalogs supplies usable griz, a one-arcsecond MAST Gaia DR3 cone supplies G, BP
and RP. Only a single source within the cone is accepted. Crowded matches,
large proper-motion/epoch offsets, unavailable services and missing photometry
produce no estimate. This deliberately favors a failed lookup over assigning
a neighboring star's flux. TIC v8 supplies **DR2** Gaia magnitudes; these are
not passed to a **DR3** transform. The extra DR3 request avoids mixing releases.

The four-band transform uses the SDSS12 relationships in ESA's
[Gaia DR3 documentation, section 5.5.1, Table 5.8](https://gea.esac.esa.int/archive/documentation/GDR3/Data_processing/chap_cu5pho/cu5pho_sec_photSystem/cu5pho_ssec_photRelations.html).
For `x = BP - RP`, each polynomial is `G - band = c0 + c1*x + ...`,
so the output is **G minus the polynomial**. The r relation is quartic;
g, i and z are cubic. Coefficients, including their signs and powers, are
stored beside the implementation in `exposure.py`. The g/r/i coefficients
also agree with [Riello et al. (2021), Appendix C, Table C.2](https://arxiv.org/abs/2012.01916).
The z relation is from the DR3 documentation, not that earlier paper.

We restrict **all four bands** to the intersection of the published color
ranges in Table 5.10: **0.5 < BP - RP < 2.0**. There is no extrapolation at
or beyond the endpoints. This avoids the red g/r regime restricted to M giants.
Invalid inputs return no estimate. The same function serves comparison stars,
whose supplied Gaia photometry must likewise be DR3/EDR3.

Results use SDSS griz as proxies for MuSCAT gp/rp/ip/zs, as with catalog
photometry. Every transformed result carries `is_approx=true`, a source label,
and an **Approximate** label on the page. The API's `approx_reason` and the
page explain the preceding lookup outcomes (including skipped CDS queries,
unavailable services, no clean match, or unusable photometry), and explicitly
say these are empirical estimates rather than measured griz. These color-based estimates have
intrinsic scatter and depend on stellar type, extinction and variability;
they are exposure-planning inputs, not precision photometric calibration or
a guarantee against saturation.

ExoFOP target requests have a 5-second timeout; successful responses are cached
for one hour and failures for 30 seconds. MAST requests have 20-second timeouts
and cache failures as well as successes. MAST uses bounded, hourly caches and the existing shared catalog
concurrency limit. No MAST name resolver (and thus no hidden CDS name lookup)
is used. A failed Sesame lookup skips the subsequent VizieR attempt.

## Time-series noise model

`muscat_db.precision` predicts the precision a star reaches at given settings
(#344). It is a library only for now: the optimizer, API and page panel that
use it come in later steps.

One frame's fractional noise is the quadrature sum of target photon noise,
sky, read noise and dark current inside the aperture, the comparison
ensemble's own noise, and scintillation. The star's count rate and the image
FWHM come from the archive-calibrated exposure model, at the median zeropoint
and median seeing. The aperture radius is 1.5 FWHM, so its area and the sky
and read noise inside it grow with defocus. Scintillation follows Osborn et
al. (2015, MNRAS 452, 1707, eq. 7), which is Young's relation times an
empirical factor (1.5 by default), and is treated as uncorrelated between the
target and each comparison star.

The figure of merit is that noise averaged over a 10-minute bin, where a bin
holds `bin / (exposure + readout overhead)` frames. Readout overheads per mode
are LCO's published values, in `exposure_model_defaults.READOUT_OVERHEAD_S`.
Read noise, sky rate, telescope diameter and site altitude are arguments, not
built-in constants. The prediction is not yet calibrated against achieved
light curves.
