# Raw data corrections

muscat-db never modifies FITS files. When raw data itself is wrong, it is
corrected once outside muscat-db, so that every consumer of our copy sees the
corrected values. The correction is made either at the data's source, or on
our copy with the sync excluded for those files so that a later sync cannot
undo it.

The original values must be kept: a backup outside the synced directory tree,
an original-value header keyword, or both. Record every correction here,
including:

- the files or nights it touched;
- each keyword changed, with its old and new value, or the rule applied;
- where the original values are kept (required);
- whether the source and any other copies were also changed, and which sync
  excludes the corrected files;
- the reason, with the issue number;
- who applied it, where, and the date.

| Date | Files or nights | Change | Originals kept in | Other copies and sync | Reason | Applied by, where |
|---|---|---|---|---|---|---|
| 2026-11-11, 2026-11-17 | MuSCAT2, 42 nights from 260723 to 261021: the 38 in the #422 table (2026-11-11), plus 260920, 261010, 261017 and 261018 (2026-11-17, offsets from the follow-up comment). Nights in the window with no measured offset are untouched: 260803, 260809, 260901, 260904, 260912, 261008 (8 frames, not usable), 261009, 261014, 261020, 261022. Two damaged frames were left uncorrected: 260907 `MCT23_2609071398.fits` and `MCT23_2609071399.fits` (unreadable headers) | the night's offset `diff` (Time_correct = Time_FITS + diff, -98.2 to +0.2 s) added to `DATE-OBS`, `MJD-STRT`, `EXP-STRT` and `READ-END`; `TIMECORR` records the offset in seconds, plus two `HISTORY` cards. `UT_STAT` and `LST` come from the telescope and are unchanged | `ODATEOBS`, `OMJDSTRT`, `OEXPSTRT`, `OREADEND` in each header; full copies in `/data/MuSCAT2/<YYMMDD>_orig`, which the scanner ignores. These sit inside the rsync target, which is safe only because the shine2 to ut2 rsync does not use `--delete` | ut2 only; shine2 and the IAC copy keep the original times. All 42 night directories are in shine2's `rsync_exclude_dates.txt` | alex2 lost NTP, timestamps off by up to about 100 s (#422) | jpdeleon, ut2, with `/data/MuSCAT2/apply_time_correction_422.py`; per-frame logs in `/data/MuSCAT2/time_correction_422_*.csv` |
