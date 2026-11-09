# Raw data corrections

muscat-db never modifies FITS files. When raw data itself is wrong, it is
corrected once at the data's source, outside muscat-db, so that every consumer
sees the corrected values and later syncs do not undo the change. Each
correction is recorded here.

For each correction, record:

- the files or nights it touched;
- each keyword changed, with its old and new value, or the rule applied;
- where the original values are kept (a header keyword, a backup location);
- the reason, with the issue number;
- who applied it, where, and the date.

| Date | Files or nights | Change | Originals kept in | Reason | Applied by, where |
|---|---|---|---|---|---|
