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
| pending | MuSCAT2, 38 nights from 260723 to 261021 | add each night's offset to the header time keywords | backup outside the data tree, plus an original-value keyword | ut2 only; shine2 and the IAC copy keep the original times; those nights are excluded from the shine2 to ut2 rsync | alex2 lost NTP, timestamps off by up to about 100 s (#422) | jpdeleon, ut2 |
