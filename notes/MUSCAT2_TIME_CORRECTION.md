# MuSCAT2 timestamp correction (2026-07-23 to 2026-10-22)

Issue: #422. Source: Fukui-san's report.

## What happened

After the IAC network policy changed in late July 2026, alex2 lost access to
its NTP server. The times in the FITS headers of MuSCAT2 data taken between
2026-07-23 and 2026-10-22 are off by up to ~100 s, which matters most for TTV
work.

## Convention

One average value per night, `diff = Time_correct - Time_FITS` in seconds. The
value is **added** to the recorded time. A negative value means the header time
was too late. The table is `MUSCAT2_CLOCK_OFFSETS` in
`src/muscat_db/time_correction.py` (38 nights).

## Where it is applied

Only when the light curves are staged for a transit fit
(`transit_fit._write_fit_inputs`): `BJD_TDB` in the copy handed to timer is
shifted. Nothing else changes:

- FITS files are not modified.
- The light-curve CSVs that prose2 wrote are not modified.
- Photometry is not rerun; prose2 still reads the uncorrected header time.
- Other instruments, and uploaded light curves (`external`), are never shifted.

Anything that reads `BJD_TDB` straight from a muscat2 light-curve CSV (plots,
time ranges, validation) still sees the uncorrected time.

## Nights without a measured offset

Nights inside the window that are not in the table are not interpolated, since
the offset steps by up to ~20 s between listed nights (260825, 260922). Their
times are left unchanged and a warning is logged naming the night.

## Existing results

Fits and TTV results for muscat2 nights in the window that were run before this
change used uncorrected times. They need to be rerun to pick up the correction.
