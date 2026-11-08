# Browse a night

In the running app, use **Obs** in the navbar. Pick an instrument, then a
date, then a CCD. That is the observation log for one night.

The tour has the same pages with mock data. Start at
[Obs](/muscatdb/home/obs/).

Photometry and transit-fit forms are in the tour too, but they do not launch
jobs.

## Viewing photometry plots

Photometry loads display-sized previews as you scroll. Click a plot to open
its original resolution; CSV and output archive downloads are unchanged.
**Master Calibration Images** starts collapsed: expand it to load those plots.
Animations start as static posters; use **Play animation** and **Stop animation**
to control downloading and playback. On a first mobile visit, chat starts
minimized; your explicit expand/minimize choice is remembered.

The browser privately caches image responses but revalidates them on each visit,
so overwriting a reduction or changing access permissions takes effect on the
next request. Display previews are PNGs cached under
`$MUSCAT_TMPDIR/image-previews` (`~/temp/image-previews` by default), with a
256 MiB disk budget and serialized generation across processes sharing that
cache. They are keyed by the source file's current metadata and requested size;
original products and FITS files are never changed. No prose2 changes are needed.
