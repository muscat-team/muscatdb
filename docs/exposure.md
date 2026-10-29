# Exposure magnitude lookup

The exposure page prefers measured Pan-STARRS DR1 / SkyMapper DR2 magnitudes.
If Sesame cannot resolve the name, or those catalogs provide no magnitudes,
it asks ExoFOP for the target's coordinates and TIC ID, then queries that exact
ID at MAST. ExoFOP only covers TESS targets; other names still rely on Sesame.
The existing name normalization and catalog lookup remain the first choice.

Measured TIC griz values take precedence over estimates, including partial
results. Missing bands are cleared from the form, so a previous target's
magnitudes cannot survive a new successful lookup. All displayed values have
at most six decimal places.

## Gaia fallback

When TIC has no usable griz, a one-arcsecond MAST Gaia DR3 cone supplies G, BP
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
and an **Approximate** label on the page. These color-based estimates have
intrinsic scatter and depend on stellar type, extinction and variability;
they are exposure-planning inputs, not precision photometric calibration or
a guarantee against saturation.

ExoFOP and MAST requests have 20-second timeouts and cached failures as well
as successes. MAST uses bounded, hourly caches and the existing shared catalog
concurrency limit. No MAST name resolver (and thus no hidden CDS name lookup)
is used. A failed Sesame lookup skips the subsequent VizieR attempt.
