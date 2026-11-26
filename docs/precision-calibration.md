# Comparing predicted and achieved MuSCAT3 precision

In the exposure calculator, **Compare predictions with achieved MuSCAT3 precision** opens a table of ten-minute precision estimates assuming white noise for archived broadband reductions. Each row reports the original prediction, the measurement and, when the training products are visible, the prediction multiplied by the fitted factor. The exposure optimizer continues to report its original noise budget.

To build a calibration, select one reduction per target, observing night and band. Use the actual band magnitudes of the target and comparison stars selected in that reduction. Verify the exposure duration, readout mode, focus offset, read noise (electrons), sky and dark rates (electrons per pixel per second) against the observing records. Use nights with consistent settings and sky conditions. Omit products with unknown inputs.

Save a JSON array with one object per product. This illustrative entry uses a path relative to the manifest. Both legacy night directories and named archive runs are supported.

```json
[{
  "instrument": "muscat3", "target": "Example", "night": "2025-01-01",
  "band": "rp", "mode": "MUSCAT_FAST",
  "csv_path": "muscat3/250101/_runs/Example/default/Example_muscat3_rp_250101.csv",
  "mag": 12.0, "comp_mags": [11.8, 12.3],
  "exptime_s": 54, "defocus_mm": 1, "read_noise_e": 5, "sky_rate": 2, "dark_rate": 0.01
}]
```

Include at least three distinct nights for each band and mode. Run `uv run muscat-db calibrate-precision products.json --db PATH` on an existing database. Each import replaces the complete sample for every included group in one transaction. Supply all intended nights for those groups on each import.

The importer orders valid frames by BJD_TDB and takes successive flux differences across the whole light curve. It skips pairs more than 1.5 exposure-plus-readout cycles apart and requires at least two eligible pairs. It divides the sample standard deviation of the differences by the median participating flux and by √2 to estimate fractional single-frame scatter. The prediction uses both endpoint variances of each accepted pair at their measured airmasses.

Both values are scaled to ten minutes assuming independent white noise, dividing by √N with N = 600 seconds / (exposure + readout). Their ratio is the same at either timescale. Older manifests can still include `oot_bjd`; the point-to-point calculation uses the whole light curve. Previously imported groups keep their original binned RMS label until reimported.

For each band and mode the newest 20% of distinct nights (rounded up) are held out. The factor is the median measured-to-predicted ratio across training products. Inspect the original and corrected predictions on the held-out nights before using a factor in observation planning.
