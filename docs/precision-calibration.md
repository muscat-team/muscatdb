# Comparing predicted and achieved MuSCAT3 precision

In the exposure calculator, **Compare predictions with achieved MuSCAT3 precision** opens a table of ten-minute RMS values for archived broadband reductions. Each row reports the original prediction, the measurement and, when the training products are visible, the prediction multiplied by the fitted factor. The exposure optimizer continues to report its original noise budget.

To build a calibration, select one reduction per target, observing night and band. Use the actual band magnitudes of the target and comparison stars selected in that reduction. Verify the exposure duration, readout mode, focus offset, read noise (electrons), sky and dark rates (electrons per pixel per second) against the observing records. Use nights with consistent settings and sky conditions. Supply out-of-transit BJD_TDB intervals checked against the target ephemeris or an independently reviewed baseline. Omit products with unknown inputs.

Save a JSON array with one object per product. This illustrative entry uses a path relative to the manifest. Both legacy night directories and named archive runs are supported.

```json
[{
  "instrument": "muscat3", "target": "Example", "night": "2025-01-01",
  "band": "rp", "mode": "MUSCAT_FAST",
  "csv_path": "muscat3/250101/_runs/Example/default/Example_muscat3_rp_250101.csv",
  "oot_bjd": [[2460677.02, 2460677.06]], "mag": 12.0, "comp_mags": [11.8, 12.3],
  "exptime_s": 54, "defocus_mm": 1, "read_noise_e": 5, "sky_rate": 2, "dark_rate": 0.01
}]
```

Include at least three distinct nights for each band and mode. Run `uv run muscat-db calibrate-precision products.json --db PATH` on an existing database. Each import replaces the complete sample for every included group in one transaction. Supply all intended nights for those groups on each import.

Bins start at each selected interval's lower boundary. The calculation discards partial intervals, bins with fewer than two frames, and bins with a gap longer than 1.5 exposure-plus-readout cycles, including gaps to a bin edge. At least two usable bins are required per product. Flux is divided by the median flux of all accepted frames. Measured RMS is the root mean square of the bin means relative to unity. The model uses the same frames and their measured airmasses, with the existing archive seeing and aperture assumptions. Independent per-frame variances are summed and divided by the squared number of frames in each bin.

For each band and mode the newest 20% of distinct nights (rounded up) are held out. The factor is the median measured-to-predicted ratio across training products. Inspect the original and corrected predictions on the held-out nights before using a factor in observation planning.
