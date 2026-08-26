# muscatdb

<!--
#[![Documentation](https://readthedocs.org/projects/quicklook/badge/?version=latest)](https://quicklook.readthedocs.io/en/latest/)
#[![PyPI](https://img.shields.io/pypi/v/muscat-db)](https://pypi.org/project/muscatdb/)
#[![Python](https://img.shields.io/pypi/pyversions/muscat-db)](https://pypi.org/project/muscatdb/)
-->
[![CI](https://github.com/muscat-team/muscatdb/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/muscat-team/muscatdb/actions/workflows/ci.yml)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/muscat-team/muscatdb)

`muscat-db` is a web-based, closed-loop exoplanet observing and analysis workflow
that integrates a central database with semi-automated, reproducible photometry,
transit, ephemeris, and TTV analyses, enabling efficient LCO scheduling with
optional FOV and exposure-time optimization and integration with external TOI
and NExScI catalogs. Supported workflow pages expose a unique, shareable view state,
allowing configurations, selections, and results to be reviewed transparently,
reproduced consistently, and communicated easily among collaborators.

## Pipeline engines

muscat-db orchestrates three external packages. Each package's code lives in its
own `ext_tools/` checkout and is installed editable into a dedicated conda
environment, which supplies its dependencies rather than the code itself — so the
checkout's git ref, not the environment, determines what actually runs. muscat-db
stores their inputs, launches them as background jobs, and renders their outputs;
the science lives in the packages themselves.

| Stage | Engine | Sampler | Outputs |
|---|---|---|---|
| Photometry | [`prose2`](https://github.com/jpdeleon/prose2) | — | Lightcurve CSVs, diagnostics |
| Transit fitting | [`timer`](https://github.com/john-livingston/timer) | PyMC (NUTS) | Posteriors, corner/trace/fit plots |
| TTV fitting | [`harmonic`](https://github.com/john-livingston/harmonic) | emcee | Posteriors, corner/trace/fit plots |

## Requirements

- Python ≥ 3.12
- FITS files below the common data root (`MUSCAT_DATA_DIR`, default `/data`) in
  `MuSCAT`, `MuSCAT2`, `MuSCAT3`, `MuSCAT4`, `Sinistro`, `SBIGSTL6303`, and
  `QHY600CMOS` subdirectories
- A writable obslog directory (`MUSCAT_OBSLOG_DIR`, default
  `$HOME/muscat/obslog`)

## Instruments

| Instrument | CCDs | FITS prefix | Data dir |
|---|---|---|---|
| muscat  | 3 | `MSCT`   | `/data/MuSCAT`   |
| muscat2 | 4 | `MCT2`   | `/data/MuSCAT2`  |
| muscat3 | 4 | `ogg2m001-` | `/data/MuSCAT3` |
| muscat4 | 4 | `coj2m002-` | `/data/MuSCAT4` |
| sinistro | 1 | `*` (any LCO 1m site) | `/data/Sinistro`  |
| sbig | 1 | `*` (any LCO 0.4m site) | `/data/SBIGSTL6303` |
| qhy600 | 1 | `*` (any LCO 0.4m site) | `/data/QHY600CMOS` |

Sinistro/sbig/qhy600 scan the reduced `*e91.fits` frames produced by LCO BANZAI, regardless of site prefix (`elp1m008-`, `coj1m003-`, `cpt1m013-`, `ogg0m406-`, `coj0m416-`, …). sbig (SBIG STL-6303, `INSTRUME` prefix `kb`) is archival-only -- LCO's live instrument API has no schedulable instrument_type for it. qhy600 (QHY600 CMOS on DeltaRho 350, `INSTRUME` prefix `sq`) is the current live 0.4m camera and is schedulable; both prefixes and the full header convention (WCS, `GAIN=1`, `CONFMODE`) are confirmed against real archived frames.

The exposure calculator uses these instrument references when scaling its
MuSCAT3 calibration. Full well is in electrons, gain in electrons/ADU, pixel
scale in arcsec/pixel, and aperture in metres.

| Instrument | Full well | Gain | Pixel scale | Aperture |
|---|---:|---:|---:|---:|
| muscat | 55,000 | 1.0 | 0.358 | 1.88 |
| muscat2 | 62,000 | 1.0 | 0.44 | 1.52 |
| muscat3 | 99,000 | 1.8 | 0.267 | 2.0 |
| muscat4 | 99,000 | 1.8 | 0.267 | 2.0 |
| sinistro | 100,000 | 1.5 | 0.39 | 1.0 |
| sbig | 102,400 | 1.0 | 0.58 | 0.4 |
| qhy600 | 47,400 | 1.0 | 0.74 | 0.35 |

## Installation and usage

Choose either workflow below. Commands elsewhere in this README use the
installed `muscat-db` executable; when using `uv`, prefix those commands with
`uv run`.

<details>
<summary><strong>uv (recommended for repository development)</strong></summary>

Install the locked project environment from the repository root:

```bash
uv sync
```

Run the CLI or start the web interface inside that environment:

```bash
uv run muscat-db --help
uv run muscat-db serve
```

</details>

<details>
<summary><strong>pip</strong></summary>

Create and activate a virtual environment, then install the project in editable
mode:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Run the installed CLI or start the web interface:

```bash
muscat-db --help
muscat-db serve
```

</details>

## Configuration

Runtime configuration is read from environment variables. Core paths and safe
operational settings have in-code defaults, while optional integrations (for
example live LCO access, ADS search, and nova astrometry) require credentials.
The central registry in `src/muscat_db/config.py` drives the startup status
report, and `.env.example` documents the common deployment settings.

On import muscat-db auto-loads a `.env` file (via `python-dotenv`,
`find_dotenv` searching upward from the working directory). A `.env` is
**optional**. When absent, `load_dotenv` is a no-op and the defaults apply.
Copy the template only when you want to override a default or pin a value:

```bash
cp .env.example .env   # then edit
```

`MUSCAT_DATA_DIR` is the common raw-data root, not one instrument's directory.
Each instrument resolves below it using its canonical case-sensitive directory
name, for example `$MUSCAT_DATA_DIR/MuSCAT3/<yymmdd>/`.

## Documentation

The published docs site is https://muscat-team.github.io/muscatdb/ (updates when
`main` is released). The UI tour is https://muscat-team.github.io/muscatdb/home/.

On a running instance, the operator Guide is `/guide`. FastAPI Swagger is `/docs`.

To preview the tour locally after building docs:

```bash
uv run mkdocs build --site-dir site
uv run muscat-db build-static-site --out site/home --db mock_muscat.db --no-figures
```

Use a real `muscat.db` on the host instead of the mock database when you want
real figures. Rebuild the snapshot after MkDocs; `mkdocs build` deletes `site/`.

## Known limitation: the UTC-midnight dataset split

`obsdate` is **not** the observing night — it is the UTC calendar date, inherited
from the directory the frames sit in. A single continuous night whose frames
straddle 00:00 UTC is therefore recorded as two datasets, and appears as two rows
on the target page.

Verified example (`TIC 460950389.01 / TOI 6715.01`, Cerro Tololo, UTC−4):

```
240417  sinistro  i  1.26–1.26    1 frame    UT 23:59:57
240418  sinistro  i  1.22–1.37  174 frames   UT 00:01:28 – 04:19:28
```

Local time 19:59 → 00:19: one continuous ~4.3 h run, cut by a UTC boundary that
has nothing to do with the night.

### Where the date comes from

The LCO archive already stamps DAY-OBS into every filename, and it is correct:
all 175 frames above are named `lsc1m004-fa03-20240417-NNNN-e91`, sequence
0092→0267 contiguous. `lco.py` `frame_destination()` ignores that token and
derives the directory from `DATE_OBS` (the actual UTC timestamp) instead, so the
post-midnight frames land in `240418`. `scanner.py` `_find_fits_files()` then
globs `<data_dir>/<obsdate>/`, and the directory name becomes the DB grouping key
verbatim.

**The correct directory for any LCO frame is therefore readable from its own
filename** — no timezone or longitude arithmetic is required to detect or repair
a split.

### Which instruments are affected

LCO-fed instruments only: sinistro, muscat3, muscat4. **muscat and muscat2 are
not affected.** MuSCAT2's telescope naming is already observing-night based —
1,079 of its 1,247 date directories contain post-midnight UT frames (e.g. dir
`200113` runs through UT 00:59:45), so its nights are never cut.

Beware of surveying this with `MIN`/`MAX` on `ut_start`: it is a time-of-day
**string**, so `MAX(ut_start)` returns the largest clock reading (23:5x) rather
than the chronologically last frame. That flags every pair of consecutive nights
that each span midnight and produces large false-positive counts. Compare
`jd_start` instead, or compare each file's DAY-OBS token to its directory:

```sql
-- authoritative: a file whose parent directory disagrees with its own DAY-OBS
SELECT instrument, obsdate, substr(filename, 15, 8) AS dayobs, COUNT(*)
FROM frames WHERE instrument IN ('sinistro','muscat3','muscat4')
GROUP BY instrument, obsdate, dayobs;
-- a directory yielding >1 distinct dayobs is a split
```

As of the 2026-07 survey, that query gives three distinct populations:

| Population | Count | Meaning |
|---|---:|---|
| Mixed dirs, adjacent day | 16 dirs | genuine midnight splits (12 sinistro, 3 muscat3, 1 muscat4) |
| Mixed dirs, non-adjacent | 4 dirs | stray/misfiled data, unrelated to midnight (e.g. `sinistro/220309` holds 181 frames dated `221209`) |
| Pure but offset | 59 dirs, 42,674 files | whole directory labelled with the UTC date rather than DAY-OBS; internally consistent, **not** split |

Only the first population causes the two-row symptom. See
[Repairing a split](#repairing-a-split-disk-consolidation) below.

### The night convention already exists elsewhere

Two other parts of the stack already use an observation-night date, which is why
this reads as an internal inconsistency rather than a missing feature:

- `lco.py` `_lco_observing_date()` does site-timezone conversion plus
  roll-back-before-local-noon — but it only feeds scheduler dataset matching,
  never the download destination.
- The photometry pipeline embeds an obs-night date in output filenames.
  `photometry.py` `discovered_targets()` and `list_outputs()` both document that
  this "may differ from the directory name (obs-night vs UT date)" and build
  regexes accepting any 6-digit date token to paper over the mismatch.

### Two constraints shaped the fix

**1. Use the filename's DAY-OBS token, not local-evening date.**
`_lco_observing_date()` is the obvious function to reach for and is the wrong
one: it returns the local *evening* date, which for western sites differs from
the current directory name for the **whole** night, not just the post-midnight
part. Adopting it would relabel every elp (26,333 frames) and ogg (115) sinistro
dataset — plus all of muscat3 — by one day.

The correct convention is the **UTC date at the start of the observing night**,
which is exactly what LCO already writes into each filename. Reading the token
merges the split nights, leaves every currently-unsplit label byte-identical, and
requires no site table.

**2. A DB-layer night key alone does not make the merged night reducible.**
`photometry.py` passes a single `--data_dir` to `run_photometry`, and prose
derives both the instrument and the obslog path from that directory's name. A
night spanning two `obsdate` directories cannot be reduced in one run by
regrouping in SQL alone — that fixes the browsing view while leaving photometry
unable to act on it. A fix has to either consolidate the frames on disk or teach
the pipeline to accept multiple input directories; the tool below takes the
disk-consolidation path.

### Repairing a split (disk consolidation)

`build-db` executes `DROP TABLE IF EXISTS frames; summaries; targets` and rebuilds
them entirely from the obslog CSV tree. App-owned tables (notes, overrides, jobs,
saved ephemeris views) are preserved; the observation-derived ones are not. **Any
edit made directly to `muscat.db` is erased by the next nightly cron run.**

The source-of-truth chain, top to bottom:

```
raw FITS      $MUSCAT_DATA_DIR/<InstDir>/<yymmdd>/*.fits        <- consolidate HERE
   |  scan_date()  -> obslog-<inst>-<yymmdd>-ccd<N>.csv, opened mode "w"
obslog CSV    $MUSCAT_OBSLOG_DIR/<inst>/<yymmdd>/
   |  _discover_csv_jobs() -> build-db
muscat.db     frames / summaries / targets    (fully derived - never edit)
```

Rule: *a file belongs in the directory matching the 8-digit DAY-OBS token in its
own filename.* This is implemented by `muscat_db/obsdate_normalize.py` and driven
by:

```bash
muscat-db normalize-obsdates             # dry run over every LCO-fed instrument
muscat-db normalize-obsdates sinistro    # dry run, one instrument
muscat-db normalize-obsdates --apply     # perform it, then rescan + re-ingest
muscat-db normalize-obsdates --apply --no-rescan
```

It is a **dry run unless `--apply` is passed**, and it reports rather than moves
anything it cannot decide safely. Each run performs the moves, deletes the obslog
CSVs they invalidate, removes directories left empty, then rescans and re-ingests
every affected date. `lco.py` `frame_destination()` now derives the download
directory from the same filename token, so new downloads do not re-create splits.

Clearing the invalidated CSVs is the step that silently undoes everything if
skipped. `scan_date` writes CSVs only for CCDs that produced rows and **never
deletes a stale CSV**, returning early when a directory holds no FITS. Move every
frame of a CCD out of a directory and its old CSV survives, `_discover_csv_jobs`
still finds it, and `build-db` re-ingests the pre-move split — the frames moved
but the database looks untouched.

The normalizer only considers files the instrument's own scanner would ingest,
reusing `scanner._find_fits_files` rather than re-deriving the glob. This matters:
the raw tree contains cross-filed data — `/data/MuSCAT3/231007` holds 1,900
`coj2m002-` (MuSCAT4) frames that no instrument scans — and relocating those would
churn files without changing anything the database sees, while still leaving them
in the wrong instrument tree.

What can erase a consolidation:

| Eraser | Mechanism | Prevention |
|---|---|---|
| `build-db` | drops and rebuilds from CSVs | consolidate in the FITS tree, never in `muscat.db` |
| stale obslog CSV | never deleted by a rescan; still ingested | delete it, or rename its dir to a non-YYMMDD name |
| `scan-missing` / `scan-all` | regenerates any raw dir whose name is not an existing obslog dir | keep raw and obslog dir names consistent; remove emptied raw dirs |
| `scan-yesterday` (cron) | scans yesterday only | historical consolidation is never revisited — this is why a one-off move survives |
| LCO re-download | `frame_destination()` uses `DATE_OBS` | fix it, or every consolidation is temporary |

Escape hatch: `_discover_csv_jobs` skips directories failing `_is_obsdate()`, so
renaming `240418` to `240418_presplit` keeps it on disk for provenance while
excluding it from `build-db`. This is an existing intentional pattern — the code
comment names `csv_old_220914` and `Hyades` as precedents.

Because the rule is filename-driven the repair is idempotent, so it can be
re-asserted rather than performed once. Running `normalize-obsdates --apply`
before `scan-yesterday` in cron corrects any split reintroduced by a re-download
or a manual copy on the next pass.

### What the normalizer refuses to do

Three classes are reported and left alone, because each needs a decision the tool
cannot make:

| Reason | Meaning | Why it is not automated |
|---|---|---|
| `offset` | every frame in the directory carries the same DAY-OBS, and no directory of that name exists | moving them is a bare directory rename, changing a dataset label that photometry output paths, `target_notes` primary keys `(object, obsdate, instrument)` and job records already reference |
| `stray` | a frame whose DAY-OBS is more than a day from its directory | misfiled data, not a midnight artifact — e.g. `sinistro/220309` holds frames dated `221209`, nine months off |
| `conflict` | the destination already holds a file of that name | the same frame exists in both directories; deleting a copy is a data-removal decision, so the tool never does it implicitly |

Conflicts are **duplicate copies of the same frame**, not competing reductions.
They are not a `.fits` / `.fits.fz` pairing either: the scanner glob is
`*e91.fits`, so a conflict only fires when the identical unpacked basename exists
in both directories.

A 450-pair sample (150 per instrument) found every pair carrying the same
`DATASUM`, `PIPEVER`, `DATE-OBS`, `DAY-OBS`, `OBJECT`, `EXPTIME` and `ORIGNAME` —
the pixel data is byte-identical. The files nonetheless differ under `md5sum`
because the FITS `CHECKSUM` card is recomputed on write; in the sampled pairs the
difference was confined to that single card, or the files were byte-identical
outright. Comparing file size or `md5sum` is therefore misleading here — compare
`DATASUM`:

```bash
python -c "
from astropy.io import fits
a,b='<dir-A>/<frame>.fits','<dir-B>/<frame>.fits'
print(fits.getheader(a).get('DATASUM'), fits.getheader(b).get('DATASUM'))"
```

Because the copies are redundant, the resolution is to delete the one in the
wrong directory and keep the one already under its DAY-OBS. That is left manual:
the normalizer relocates frames but never deletes science data.

A directory holding more than one DAY-OBS is always repaired: two nights sharing
one directory is unambiguously wrong, so the minority moves even when the
destination has to be created. A directory whose frames all agree with each other
is only repaired when that night already has a home elsewhere.

As of the 2026-07 dry run: **10,328 frames** would move (sinistro 7,427 across 17
directory pairs, muscat3 2,861 across 3, muscat4 40 across 1), with 28 `offset`,
6 `stray` and 7,083 `conflict` reports left for review.
